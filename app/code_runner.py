"""Safe multi-language code execution engine and sandbox management."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
try:
    import psutil
except ImportError:
    psutil = None
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field


class ExecutionRequest(BaseModel):
    language: str = Field(description="Language identifier (python, javascript, typescript, c, cpp, bash, go, rust, php, ruby)")
    code: Optional[str] = Field(default=None, description="Raw code to execute (if running unsaved snippet)")
    filename: Optional[str] = Field(default=None, description="Filename (e.g. main.py, index.js)")
    stdin: Optional[str] = Field(default="", description="Input to pass to STDIN")
    files: Optional[Dict[str, str]] = Field(default_factory=dict, description="Additional workspace files {rel_path: content}")
    timeout: Optional[float] = Field(default=15.0, ge=1.0, le=60.0, description="Max execution time in seconds")


class ExecutionResult(BaseModel):
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    execution_time_ms: float
    memory_peak_kb: Optional[int] = None
    timed_out: bool = False
    error: Optional[str] = None


LANGUAGE_CONFIG: Dict[str, Dict[str, Any]] = {
    "python": {
        "extension": ".py",
        "command": ["python3", "-u"],
        "default_filename": "main.py",
    },
    "javascript": {
        "extension": ".js",
        "command": ["node"],
        "default_filename": "index.js",
    },
    "typescript": {
        "extension": ".ts",
        "command": ["node"],  # or ts-node/tsx if installed, fallback to node
        "default_filename": "index.ts",
    },
    "bash": {
        "extension": ".sh",
        "command": ["bash"],
        "default_filename": "script.sh",
    },
    "sh": {
        "extension": ".sh",
        "command": ["sh"],
        "default_filename": "script.sh",
    },
    "c": {
        "extension": ".c",
        "compile_cmd": ["gcc", "-O2"],
        "default_filename": "main.c",
    },
    "cpp": {
        "extension": ".cpp",
        "compile_cmd": ["g++", "-std=c++17", "-O2"],
        "default_filename": "main.cpp",
    },
    "go": {
        "extension": ".go",
        "command": ["go", "run"],
        "default_filename": "main.go",
    },
    "rust": {
        "extension": ".rs",
        "compile_cmd": ["rustc", "-O"],
        "default_filename": "main.rs",
    },
    "php": {
        "extension": ".php",
        "command": ["php"],
        "default_filename": "index.php",
    },
    "ruby": {
        "extension": ".rb",
        "command": ["ruby"],
        "default_filename": "main.rb",
    },
}


def get_available_runtimes() -> List[Dict[str, Any]]:
    """Scan host environment to discover which language compilers and interpreters are installed."""
    runtimes = []
    checks = [
        ("python", "Python 3", "python3", ["python3", "--version"]),
        ("javascript", "Node.js", "node", ["node", "--version"]),
        ("typescript", "TypeScript (Node)", "node", ["node", "--version"]),
        ("bash", "Bash Shell", "bash", ["bash", "--version"]),
        ("c", "GCC (C)", "gcc", ["gcc", "--version"]),
        ("cpp", "G++ (C++)", "g++", ["g++", "--version"]),
        ("go", "Go Lang", "go", ["go", "version"]),
        ("rust", "Rust Compiler", "rustc", ["rustc", "--version"]),
        ("php", "PHP", "php", ["php", "--version"]),
        ("ruby", "Ruby", "ruby", ["ruby", "--version"]),
    ]

    for lang_id, display_name, binary_name, version_cmd in checks:
        binary_path = shutil.which(binary_name)
        if binary_path:
            version_str = "Installed"
            try:
                import subprocess
                res = subprocess.run(version_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=2)
                if res.returncode == 0 and res.stdout:
                    version_str = res.stdout.strip().split("\n")[0]
            except Exception:
                pass
            runtimes.append({
                "id": lang_id,
                "name": display_name,
                "installed": True,
                "path": binary_path,
                "version": version_str
            })
        else:
            runtimes.append({
                "id": lang_id,
                "name": display_name,
                "installed": False,
                "path": None,
                "version": "Not installed"
            })
    return runtimes


async def execute_code(request: ExecutionRequest) -> ExecutionResult:
    """Safely execute code in an ephemeral isolated directory with resource constraints."""
    lang_key = request.language.lower().strip()
    config = LANGUAGE_CONFIG.get(lang_key)

    if not config:
        return ExecutionResult(
            success=False,
            stdout="",
            stderr=f"Unsupported language: '{request.language}'. Supported languages: {', '.join(LANGUAGE_CONFIG.keys())}",
            exit_code=1,
            execution_time_ms=0,
        )

    # Determine primary filename
    filename = request.filename or config.get("default_filename") or f"main{config['extension']}"
    
    # Create isolated temp workspace directory
    temp_dir = tempfile.mkdtemp(prefix="codespace_exec_")
    
    try:
        # Write additional workspace files if provided
        if request.files:
            for rel_path, content in request.files.items():
                safe_rel_path = os.path.normpath(rel_path).lstrip("/\\")
                if ".." in safe_rel_path:
                    continue  # Protect against path traversal
                full_path = os.path.join(temp_dir, safe_rel_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)

        # Write primary code file
        primary_file_path = os.path.join(temp_dir, filename)
        if request.code is not None:
            with open(primary_file_path, "w", encoding="utf-8") as f:
                f.write(request.code)
        elif not os.path.exists(primary_file_path):
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="No code or file content provided for execution.",
                exit_code=1,
                execution_time_ms=0,
            )

        # Handle compilation step if required (e.g. C, C++, Rust)
        start_time = time.perf_counter()
        if "compile_cmd" in config:
            compiler = config["compile_cmd"][0]
            if not shutil.which(compiler):
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr=f"Compiler '{compiler}' is not installed on this server.",
                    exit_code=127,
                    execution_time_ms=0,
                )
            
            output_binary = os.path.join(temp_dir, "app_bin")
            compile_args = [*config["compile_cmd"], primary_file_path, "-o", output_binary]
            
            compile_proc = await asyncio.create_subprocess_exec(
                *compile_args,
                cwd=temp_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            c_out, c_err = await compile_proc.communicate()
            if compile_proc.returncode != 0:
                return ExecutionResult(
                    success=False,
                    stdout=c_out.decode("utf-8", errors="replace"),
                    stderr=f"Compilation Failed:\n" + c_err.decode("utf-8", errors="replace"),
                    exit_code=compile_proc.returncode or 1,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                )
            run_cmd = [output_binary]
        else:
            runner = config["command"][0]
            if not shutil.which(runner):
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr=f"Runtime interpreter '{runner}' is not installed on this server.",
                    exit_code=127,
                    execution_time_ms=0,
                )
            run_cmd = [*config["command"], filename]

        # Execute the process with timeout and input
        exec_start = time.perf_counter()
        stdin_bytes = request.stdin.encode("utf-8") if request.stdin else None
        
        proc = await asyncio.create_subprocess_exec(
            *run_cmd,
            cwd=temp_dir,
            stdin=asyncio.subprocess.PIPE if stdin_bytes else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
        )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=request.timeout or 15.0
            )
        except asyncio.TimeoutError:
            timed_out = True
            if psutil:
                try:
                    # Terminate tree if process is still running
                    parent = psutil.Process(proc.pid)
                    for child in parent.children(recursive=True):
                        child.kill()
                    parent.kill()
                except Exception:
                    pass
            try:
                proc.kill()
            except Exception:
                pass
            stdout_bytes, stderr_bytes = b"", f"Execution timed out after {request.timeout} seconds.\n".encode("utf-8")

        elapsed_ms = (time.perf_counter() - exec_start) * 1000
        
        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")
        
        # Limit output size to prevent UI freeze (max 500KB)
        max_chars = 500_000
        if len(stdout_str) > max_chars:
            stdout_str = stdout_str[:max_chars] + "\n... [Output truncated: exceeds maximum display limit]"
        if len(stderr_str) > max_chars:
            stderr_str = stderr_str[:max_chars] + "\n... [Error output truncated]"

        return ExecutionResult(
            success=(proc.returncode == 0 and not timed_out),
            stdout=stdout_str,
            stderr=stderr_str,
            exit_code=-1 if timed_out else (proc.returncode or 0),
            execution_time_ms=round(elapsed_ms, 2),
            timed_out=timed_out,
        )

    except Exception as e:
        return ExecutionResult(
            success=False,
            stdout="",
            stderr=f"Execution error: {str(e)}",
            exit_code=1,
            execution_time_ms=0,
            error=str(e),
        )
    finally:
        # Clean up temporary execution directory
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass
