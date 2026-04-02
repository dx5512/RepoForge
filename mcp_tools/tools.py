"""
MCP Tools Implementation - Shared Library

This module contains the actual tool implementations that can be used by:
1. MCP Server (mcp_tools/main.py) - via MCP protocol
2. Direct local caller (mcp_client.py) - via direct function calls

This separation enables decoupling: the main application doesn't directly
import sandbox modules, but uses these tool functions instead.
"""

import asyncio
from pathlib import Path
from typing import Tuple, Optional, Any

from sandbox import TaskRegistry, SandboxController
from sandbox.interceptors import CommandInterceptor
from sandbox.worktree import WorktreeManager

# Global registry map: worktrees_base path -> TaskRegistry
_task_registries: dict = {}

def get_registry(worktrees_base: Path) -> TaskRegistry:
    """Get or create TaskRegistry for specific worktrees_base."""
    global _task_registries
    key = str(worktrees_base.resolve())
    if key not in _task_registries:
        _task_registries[key] = TaskRegistry(worktrees_base)
    return _task_registries[key]


def get_sandbox_for_task(task_id: int, registry: TaskRegistry) -> Tuple[Optional[SandboxController], Optional[str]]:
    """Get sandbox for task_id or return error."""
    sandbox = registry.get(task_id)

    if sandbox is None:
        return None, f"No sandbox found for task_id={task_id}. Create worktree first."
    if not sandbox.is_running():
        return None, f"Sandbox for task_id={task_id} is not running"
    return sandbox, None


# ========== File Tools ==========

def tool_file_read(task_id: int, path: str, limit: int = None, worktrees_base: Path = None) -> str:
    """Read file from sandbox worktree."""
    if worktrees_base is None:
        worktrees_base = Path(".worktrees")
    registry = get_registry(worktrees_base)
    sandbox, error = get_sandbox_for_task(task_id, registry)
    if error:
        return f"Error: {error}"

    safe_path = sandbox.worktree_path / path
    if not safe_path.exists():
        return f"Error: File not found: {path}"

    try:
        content = safe_path.read_text(encoding="utf-8")
        if limit:
            lines = content.splitlines()
            content = "\n".join(lines[:limit])
        return content
    except Exception as e:
        return f"Error: {e}"


def tool_file_write(task_id: int, path: str, content: str, worktrees_base: Path = None) -> str:
    """Write content to file in sandbox worktree."""
    if worktrees_base is None:
        worktrees_base = Path(".worktrees")
    registry = get_registry(worktrees_base)
    sandbox, error = get_sandbox_for_task(task_id, registry)
    if error:
        return f"Error: {error}"

    safe_path = sandbox.worktree_path / path

    try:
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(content, encoding="utf-8")
        return f"OK: Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# ========== Bash Tools ==========

def tool_bash_execute(
    task_id: int,
    command: str,
    timeout: int = 120,
    worktrees_base: Path = None,
    interceptor: CommandInterceptor = None
) -> str:
    """Execute command in sandbox."""
    if worktrees_base is None:
        worktrees_base = Path(".worktrees")
    if interceptor is None:
        interceptor = CommandInterceptor()

    registry = get_registry(worktrees_base)
    sandbox, error = get_sandbox_for_task(task_id, registry)
    if error:
        return f"Error: {error}"

    safe, reason = interceptor.is_safe(command)
    if not safe:
        return f"Error: Blocked: {reason}"

    result = sandbox.execute_in_sandbox(command, timeout)

    output = []
    if result["stdout"]:
        output.append(f"STDOUT:\n{result['stdout']}")
    if result["stderr"]:
        output.append(f"STDERR:\n{result['stderr']}")
    output.append(f"EXIT CODE: {result['exit_code']}")
    if result.get("error"):
        output.append(f"ERROR: {result['error']}")

    return "\n".join(output)


# ========== Worktree Tools ==========

def tool_worktree_create(
    task_id: int,
    name: str,
    base_ref: str = "HEAD",
    workdir: Path = None,
    worktrees_base: Path = None
) -> str:
    """Create a worktree and sandbox for a task."""
    if workdir is None:
        workdir = Path.cwd()
    if worktrees_base is None:
        worktrees_base = workdir / ".worktrees"

    registry = get_registry(worktrees_base)

    if registry.get(task_id):
        return f"Error: Sandbox already exists for task_id={task_id}"

    wtm = WorktreeManager(workdir, worktrees_base)

    try:
        wt_info = wtm.create(name, task_id, base_ref)
        wt_path = Path(wt_info["path"])

        sandbox = SandboxController()
        sandbox.create_container(task_id, wt_path)
        registry.register(task_id, sandbox)

        return f"OK: Created worktree '{name}' for task {task_id}"
    except Exception as e:
        return f"Error: {e}"


def tool_worktree_list(workdir: Path = None, worktrees_base: Path = None) -> str:
    """List all worktrees."""
    if workdir is None:
        workdir = Path.cwd()
    if worktrees_base is None:
        worktrees_base = workdir / ".worktrees"

    wtm = WorktreeManager(workdir, worktrees_base)
    worktrees = wtm.list_all()

    if not worktrees:
        return "No worktrees"

    lines = []
    for wt in worktrees:
        lines.append(f"- {wt['name']}: {wt['path']} (task={wt.get('task_id')}, status={wt.get('status')})")
    return "\n".join(lines)


def tool_worktree_remove(
    task_id: int,
    name: str = None,
    keep_files: bool = False,
    workdir: Path = None,
    worktrees_base: Path = None
) -> str:
    """Remove worktree and sandbox for a task."""
    if workdir is None:
        workdir = Path.cwd()
    if worktrees_base is None:
        worktrees_base = workdir / ".worktrees"

    registry = get_registry(worktrees_base)
    wtm = WorktreeManager(workdir, worktrees_base)

    if name is None:
        wt_info = wtm.get_by_task(task_id)
        if wt_info:
            name = wt_info["name"]

    sandbox = registry.unregister(task_id)

    if name:
        try:
            if keep_files:
                wtm.keep(name)
            else:
                wtm.remove(name, force=True)
        except Exception as e:
            return f"Warning: {e}"

    return f"OK: Removed sandbox for task {task_id}"


# ========== Sandbox Controller Tools (Special) ==========

def tool_sandbox_create_container(task_id: int, worktree_path: Path, worktrees_base: Path = None) -> str:
    """
    Create a Docker container for a task.
    This is a special operation that registers the sandbox with the registry.
    """
    if worktrees_base is None:
        worktrees_base = Path(".worktrees")

    sandbox = SandboxController()
    sandbox.create_container(task_id, worktree_path)

    # Register with global registry using correct worktrees_base
    registry = get_registry(worktrees_base)
    registry.register(task_id, sandbox)

    return f"OK: Container created for task {task_id}"


def tool_sandbox_destroy_container(task_id: int, worktrees_base: Path = None) -> str:
    """Destroy the sandbox container for a task."""
    if worktrees_base is None:
        worktrees_base = Path(".worktrees")
    registry = get_registry(worktrees_base)
    sandbox = registry.unregister(task_id)
    if sandbox:
        return f"OK: Container destroyed for task {task_id}"
    return f"OK: No container found for task {task_id}"


# ========== Utility Functions ==========

def cleanup_all_sandboxes(workdir: Path = None, worktrees_base: Path = None) -> int:
    """Destroy all registered sandboxes. Returns count of cleaned up tasks."""
    if workdir is None:
        workdir = Path.cwd()
    if worktrees_base is None:
        worktrees_base = workdir / ".worktrees"

    registry = get_registry(worktrees_base)
    count = registry.cleanup_all()
    return count
