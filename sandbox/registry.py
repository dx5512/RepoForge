"""
TaskRegistry - Maps task_id to SandboxController instances

CRITICAL: This enables stateless MCP tools to route operations to the correct
sandbox based on task_id (State Handoff fix).

The MCP server receives task_id in every request and uses this registry
to find the corresponding SandboxController instance.
"""

from typing import Dict, Optional
from pathlib import Path
import logging
import threading

from .container import SandboxController

logger = logging.getLogger(__name__)


class TaskRegistry:
    """
    Maintains mapping of task_id -> SandboxController instance.

    This is the key component that enables stateless MCP tool design:
    - MCP tools receive task_id with every request
    - MCP tools query TaskRegistry to get the correct SandboxController
    - Each task gets isolated sandbox + worktree
    """

    def __init__(self, worktrees_base: Path):
        self.worktrees_base = worktrees_base
        self._registry: Dict[int, SandboxController] = {}
        self._lock = threading.Lock()  # 🔒 Protect all registry operations

    def register(self, task_id: int, sandbox: SandboxController) -> None:
        """Register a sandbox for a task."""
        with self._lock:
            self._registry[task_id] = sandbox

    def get(self, task_id: int) -> Optional[SandboxController]:
        """Get sandbox for a task_id."""
        with self._lock:
            return self._registry.get(task_id)

    def unregister(self, task_id: int) -> Optional[SandboxController]:
        """
        Unregister and destroy sandbox for a task.
        Returns the destroyed sandbox for cleanup verification.
        """
        with self._lock:
            sandbox = self._registry.pop(task_id, None)
        if sandbox:
            sandbox.destroy_container()
        return sandbox

    def list_tasks(self) -> Dict[int, dict]:
        """List all registered tasks with their sandbox info."""
        with self._lock:
            result = {}
            for task_id, sandbox in self._registry.items():
                result[task_id] = sandbox.get_container_info()
        return result

    def get_worktree_path(self, task_id: int) -> Optional[Path]:
        """Get worktree path for a task."""
        with self._lock:
            sandbox = self._registry.get(task_id)
        if sandbox:
            return sandbox.worktree_path
        return None

    def cleanup_all(self) -> int:
        """
        Destroy all registered sandboxes.
        Returns count of cleaned up tasks.
        """
        with self._lock:
            count = len(self._registry)
            task_ids = list(self._registry.keys())
            # Create copies of sandbox objects before unlocking
            sandboxes = [self._registry[tid] for tid in task_ids]
            # Clear registry while holding lock
            self._registry.clear()

        # Destroy sandboxes outside the lock to avoid deadlock
        for sandbox in sandboxes:
            try:
                sandbox.destroy_container()
            except Exception as e:
                logger.warning(f"Error destroying sandbox during cleanup: {e}")

        return count

    def __len__(self) -> int:
        with self._lock:
            return len(self._registry)
