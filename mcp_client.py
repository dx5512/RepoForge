"""
MCP Client Wrapper for RepoForge

Provides a simplified interface for calling MCP tools without dealing with
the full MCP protocol. This enables decoupling: the main app doesn't
directly import sandbox modules.

Two usage modes:
1. Direct function calls (synchronous)
2. MCP stdio protocol (for external clients)
"""

import asyncio
import threading
from pathlib import Path
from typing import Optional, Dict, Any

from mcp_tools.tools import (
    tool_file_read,
    tool_file_write,
    tool_bash_execute,
    tool_worktree_create,
    tool_worktree_list,
    tool_worktree_remove,
    tool_sandbox_create_container,
    tool_sandbox_destroy_container,
    cleanup_all_sandboxes,
    get_registry,
)

from config import get_config

config = get_config()


class MCPClient:
    """
    Client for calling MCP tools directly (without MCP protocol overhead).

    This class wraps the tool functions and provides a clean interface for
    the main application to perform sandbox operations without importing
    sandbox modules directly.
    """

    def __init__(self, workdir: Path = None, worktrees_base: Path = None):
        """
        Initialize MCP client.

        Args:
            workdir: Working directory (default: current directory)
            worktrees_base: Base path for worktrees (default: .worktrees)
        """
        self.workdir = workdir or Path.cwd()
        self.worktrees_base = worktrees_base or (self.workdir / ".worktrees")
        self.worktrees_base.mkdir(exist_ok=True)

    # ========== File Operations ==========

    def file_read(self, task_id: int, path: str, limit: int = None) -> str:
        """Read file from sandbox worktree."""
        return tool_file_read(task_id, path, limit, self.worktrees_base)

    def file_write(self, task_id: int, path: str, content: str) -> str:
        """Write content to file in sandbox worktree."""
        return tool_file_write(task_id, path, content, self.worktrees_base)

    # ========== Bash Execution ==========

    def bash_execute(self, task_id: int, command: str, timeout: int = 120) -> str:
        """Execute command in sandbox."""
        return tool_bash_execute(task_id, command, timeout, self.worktrees_base)

    # ========== Worktree Management ==========

    def worktree_create(self, task_id: int, name: str, base_ref: str = "HEAD", workdir: Path = None) -> str:
        """Create a worktree and sandbox for a task."""
        if workdir is None:
            workdir = self.workdir
        return tool_worktree_create(task_id, name, base_ref, workdir, self.worktrees_base)

    def worktree_list(self, workdir: Path = None) -> str:
        """List all worktrees."""
        if workdir is None:
            workdir = self.workdir
        return tool_worktree_list(workdir, self.worktrees_base)

    def worktree_remove(self, task_id: int, name: str = None, keep_files: bool = False, workdir: Path = None) -> str:
        """Remove worktree and sandbox for a task."""
        if workdir is None:
            workdir = self.workdir
        return tool_worktree_remove(task_id, name, keep_files, workdir, self.worktrees_base)

    # ========== Sandbox Management ==========

    def sandbox_create_container(self, task_id: int, worktree_path: Path) -> str:
        """Create a Docker container for a task (explicit operation)."""
        return tool_sandbox_create_container(task_id, worktree_path, self.worktrees_base)

    def sandbox_destroy_container(self, task_id: int) -> str:
        """Destroy the sandbox container for a task."""
        return tool_sandbox_destroy_container(task_id, self.worktrees_base)

    # ========== Cleanup ==========

    def cleanup_all(self) -> int:
        """Destroy all registered sandboxes. Returns count cleaned."""
        return cleanup_all_sandboxes(self.workdir, self.worktrees_base)

    # ========== Registry Access ==========

    def get_registry(self):
        """Get the global TaskRegistry for advanced operations."""
        return get_registry(self.worktrees_base)


# Global client instance (singleton pattern)
_global_client: Optional[MCPClient] = None
_global_client_lock = threading.Lock()  # 🔒 Thread-safe singleton


def get_mcp_client() -> MCPClient:
    """
    Get the global MCP client instance.

    This provides a singleton that can be imported and used throughout
    the application without passing around instances.

    Returns:
        MCPClient instance
    """
    global _global_client
    if _global_client is None:
        with _global_client_lock:
            # Double-checked locking pattern
            if _global_client is None:
                _global_client = MCPClient()
    return _global_client


# ========== Convenience Functions ==========
# For quick one-liners without creating a client

def mcp_file_read(task_id: int, path: str, limit: int = None) -> str:
    """Convenience function for file_read."""
    return get_mcp_client().file_read(task_id, path, limit)


def mcp_file_write(task_id: int, path: str, content: str) -> str:
    """Convenience function for file_write."""
    return get_mcp_client().file_write(task_id, path, content)


def mcp_bash_execute(task_id: int, command: str, timeout: int = 120) -> str:
    """Convenience function for bash_execute."""
    return get_mcp_client().bash_execute(task_id, command, timeout)


def mcp_worktree_create(task_id: int, name: str, base_ref: str = "HEAD") -> str:
    """Convenience function for worktree_create."""
    return get_mcp_client().worktree_create(task_id, name, base_ref)


def mcp_worktree_remove(task_id: int, name: str = None, keep_files: bool = False) -> str:
    """Convenience function for worktree_remove."""
    return get_mcp_client().worktree_remove(task_id, name, keep_files)


if __name__ == "__main__":
    # Quick test
    print("MCP Client module loaded successfully")
    print("Available functions:")
    print("  - file_read, file_write, bash_execute")
    print("  - worktree_create, worktree_list, worktree_remove")
    print("  - sandbox_create_container, sandbox_destroy_container")
    print("  - cleanup_all")
