"""
WorktreeManager - Git Worktree Lifecycle Management

Based on s12_worktree_task_isolation.py logic.
Provides directory-level isolation for parallel task execution.
"""

import json
import re
import subprocess
import time
import shutil
import os
import stat
from pathlib import Path
from typing import Optional


def _remove_readonly(func, path, _):
    """Clear the readonly bit and reattempt the removal."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def robust_rmtree(path, max_retries=3, logger=None):
    """
    Robustly remove a directory tree on Windows.
    
    Handles read-only files, file locks, and Docker mount issues.
    """
    for i in range(max_retries):
        try:
            if logger:
                logger.info(f"Attempting to remove {path} (attempt {i+1}/{max_retries})")
            shutil.rmtree(path, onerror=_remove_readonly)
            if logger:
                logger.info(f"Successfully removed {path}")
            return True
        except Exception as e:
            if logger:
                logger.warning(f"Failed to remove {path} (attempt {i+1}/{max_retries}): {e}")
            if i < max_retries - 1:
                time.sleep(2)
            else:
                if logger:
                    logger.error(f"Failed to remove {path} after {max_retries} attempts: {e}")
                return False
    return False


class WorktreeManager:
    """
    Manages Git worktrees for task isolation.

    Key design: "Isolate by directory, coordinate by task ID"
    """

    def __init__(self, repo_root: Path, worktrees_base: Path):
        self.repo_root = repo_root
        self.worktrees_base = worktrees_base
        self.worktrees_base.mkdir(parents=True, exist_ok=True)
        self.index_path = worktrees_base / "index.json"
        if not self.index_path.exists():
            self._save_index({"worktrees": []})
        self.git_available = self._is_git_repo()

    def _is_git_repo(self) -> bool:
        """Check if we're in a git repository."""
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _run_git(self, args: list) -> str:
        """Run a git command."""
        if not self.git_available:
            raise RuntimeError("Not in a git repository")
        r = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            msg = (r.stdout + r.stderr).strip()
            raise RuntimeError(msg or f"git {' '.join(args)} failed")
        return (r.stdout + r.stderr).strip() or "(no output)"

    def _load_index(self) -> dict:
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _save_index(self, data: dict):
        self.index_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _find(self, name: str) -> Optional[dict]:
        idx = self._load_index()
        for wt in idx.get("worktrees", []):
            if wt.get("name") == name:
                return wt
        return None

    def _validate_name(self, name: str):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):
            raise ValueError("Invalid worktree name. Use 1-40 chars: letters, numbers, ., _, -")

    def create(self, name: str, task_id: int, base_ref: str = "HEAD") -> dict:
        """
        Create a new worktree for a task.

        Args:
            name: Worktree directory name
            task_id: Associated task ID
            base_ref: Git reference to base the worktree on

        Returns:
            dict with worktree metadata
        """
        self._validate_name(name)
        if self._find(name):
            raise ValueError(f"Worktree '{name}' already exists")

        path = self.worktrees_base / name
        branch = f"wt/{name}"

        self._run_git(["worktree", "add", "-b", branch, str(path), base_ref])

        entry = {
            "name": name,
            "path": str(path),
            "branch": branch,
            "task_id": task_id,
            "status": "active",
            "created_at": time.time(),
        }

        idx = self._load_index()
        idx["worktrees"].append(entry)
        self._save_index(idx)

        return entry

    def list_all(self) -> list:
        """List all worktrees."""
        idx = self._load_index()
        return idx.get("worktrees", [])

    def get(self, name: str) -> Optional[dict]:
        """Get worktree by name."""
        return self._find(name)

    def get_by_task(self, task_id: int) -> Optional[dict]:
        """Get worktree bound to a task."""
        for wt in self.list_all():
            if wt.get("task_id") == task_id:
                return wt
        return None

    def status(self, name: str) -> str:
        """Get git status of a worktree."""
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"

        path = Path(wt["path"])
        if not path.exists():
            return f"Error: Worktree path missing: {path}"

        r = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return (r.stdout + r.stderr).strip() or "Clean worktree"

    def remove(self, name: str, force: bool = False) -> str:
        """Remove a worktree."""
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(wt["path"])

        self._run_git(args)

        idx = self._load_index()
        for item in idx.get("worktrees", []):
            if item.get("name") == name:
                item["status"] = "removed"
                item["removed_at"] = time.time()
        self._save_index(idx)

        return f"Removed worktree '{name}'"

    def keep(self, name: str) -> dict:
        """Mark a worktree as kept (preserved after task completion)."""
        wt = self._find(name)
        if not wt:
            raise ValueError(f"Unknown worktree '{name}'")

        idx = self._load_index()
        for item in idx.get("worktrees", []):
            if item.get("name") == name:
                item["status"] = "kept"
                item["kept_at"] = time.time()
        self._save_index(idx)

        return wt
