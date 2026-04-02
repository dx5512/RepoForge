"""
Unit tests for WorktreeManager

Tests git worktree lifecycle management.
Note: These tests require git to be installed and operate on a temporary git repository.
"""

import pytest
import subprocess
from pathlib import Path
import tempfile
import shutil
import time
import os
import stat

from sandbox.worktree import WorktreeManager, robust_rmtree


@pytest.fixture
def temp_git_repo(tmp_path):
    """Create a temporary git repository for testing."""
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True)

    # Create an initial commit
    test_file = repo_path / "test.txt"
    test_file.write_text("initial content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_path, check=True)

    yield repo_path

    # Cleanup: remove all worktrees (handled by WorktreeManager.remove)
    # But ensure no lingering worktrees
    try:
        subprocess.run(["git", "worktree", "prune"], cwd=repo_path, check=True, capture_output=True)
    except:
        pass


@pytest.fixture
def worktree_manager(temp_git_repo):
    """Create a WorktreeManager instance."""
    worktrees_base = temp_git_repo.parent / "worktrees"
    wtm = WorktreeManager(temp_git_repo, worktrees_base)
    return wtm


class TestWorktreeManager:
    """Test suite for WorktreeManager."""

    def test_initialization(self, worktree_manager, temp_git_repo):
        """Test WorktreeManager initializes correctly."""
        assert worktree_manager.repo_root == temp_git_repo
        assert worktree_manager.worktrees_base.exists()
        assert worktree_manager.git_available is True

    def test_create_worktree(self, worktree_manager):
        """Test creating a new worktree."""
        wt_info = worktree_manager.create(name="test_wt", task_id=123, base_ref="HEAD")

        assert wt_info["name"] == "test_wt"
        assert wt_info["task_id"] == 123
        assert wt_info["status"] == "active"
        assert Path(wt_info["path"]).exists()
        assert Path(wt_info["path"]).is_dir()

        # Verify the worktree directory contains .git
        git_dir = Path(wt_info["path"]) / ".git"
        assert git_dir.exists() or (Path(wt_info["path"]) / ".git").is_file() or \
               Path(wt_info["path"]).joinpath(".git").exists()  # Git worktree structure

    def test_list_worktrees(self, worktree_manager):
        """Test listing all worktrees."""
        # Initially should be empty or only existing worktrees
        initial = worktree_manager.list_all()
        initial_count = len(initial)

        # Create a worktree
        worktree_manager.create(name="wt1", task_id=1)

        all_wt = worktree_manager.list_all()
        assert len(all_wt) == initial_count + 1
        assert any(wt["name"] == "wt1" for wt in all_wt)

    def test_get_worktree_by_name(self, worktree_manager):
        """Test getting a worktree by name."""
        worktree_manager.create(name="wt2", task_id=2)
        wt = worktree_manager.get("wt2")

        assert wt is not None
        assert wt["name"] == "wt2"
        assert wt["task_id"] == 2

    def test_get_worktree_by_task(self, worktree_manager):
        """Test getting a worktree by task ID."""
        worktree_manager.create(name="wt3", task_id=456)
        wt = worktree_manager.get_by_task(456)

        assert wt is not None
        assert wt["name"] == "wt3"

    def test_remove_worktree(self, worktree_manager):
        """Test removing a worktree."""
        wt_info = worktree_manager.create(name="wt_to_remove", task_id=999)
        wt_path = Path(wt_info["path"])

        # Ensure it exists
        assert wt_path.exists()

        # Remove it
        result = worktree_manager.remove("wt_to_remove", force=True)
        assert "Removed" in result

        # Verify it's gone from index
        assert worktree_manager.get("wt_to_remove") is None

        # Physical directory might still exist briefly due to git worktree implementation
        # But index should be updated

    def test_duplicate_worktree_name_fails(self, worktree_manager):
        """Test that creating a worktree with duplicate name raises error."""
        worktree_manager.create(name="dup_wt", task_id=100)

        with pytest.raises(ValueError, match="already exists"):
            worktree_manager.create(name="dup_wt", task_id=101)

    def test_invalid_worktree_name(self, worktree_manager):
        """Test that invalid worktree names are rejected."""
        invalid_names = [
            "name with spaces",
            "name@at",
            "name#hash",
            "a" * 50,  # too long (max 40)
            "",  # empty
        ]

        for name in invalid_names:
            with pytest.raises(ValueError):
                worktree_manager.create(name=name, task_id=1)

    def test_worktree_status(self, worktree_manager):
        """Test getting git status of a worktree."""
        wt_info = worktree_manager.create(name="status_test", task_id=200)
        wt_name = wt_info["name"]

        status = worktree_manager.status(wt_name)
        # Should show clean worktree or branch info
        assert isinstance(status, str)
        # Should not be an error message
        assert not status.startswith("Error:")

    def test_robust_rmtree(self, tmp_path):
        """Test robust directory removal."""
        # Create a directory with read-only files
        test_dir = tmp_path / "test_rm"
        test_dir.mkdir()
        test_file = test_dir / "readonly.txt"
        test_file.write_text("content")
        # Make file read-only (cross-platform)
        import stat
        os.chmod(test_file, stat.S_IREAD)

        # Should be able to remove even with readonly files
        result = robust_rmtree(str(test_dir), max_retries=3)
        assert result is True
        assert not test_dir.exists()

    def test_multiple_worktrees_isolation(self, worktree_manager):
        """Test that multiple worktrees are properly isolated."""
        wt1 = worktree_manager.create(name="isolate1", task_id=1)
        wt2 = worktree_manager.create(name="isolate2", task_id=2)

        # They should have different paths
        assert wt1["path"] != wt2["path"]
        assert Path(wt1["path"]).exists()
        assert Path(wt2["path"]).exists()

        # Their branch names should be different
        assert wt1["branch"] != wt2["branch"]

        # Removing one should not affect the other
        worktree_manager.remove("isolate1", force=True)

        assert worktree_manager.get("isolate1") is None
        assert worktree_manager.get("isolate2") is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
