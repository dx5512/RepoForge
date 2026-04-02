"""
Unit tests for CommandInterceptor

Tests the dangerous command blocking and path validation logic.
"""

import pytest
from pathlib import Path
import tempfile
import shutil

from sandbox.interceptors import CommandInterceptor


class TestCommandInterceptor:
    """Test suite for CommandInterceptor."""

    @pytest.fixture
    def interceptor(self):
        """Create a fresh interceptor instance."""
        return CommandInterceptor()

    @pytest.mark.parametrize("dangerous_cmd", [
        "rm -rf /",
        "rm -rf /*",
        "rm -rf *",
        "rm -rf ~",
        "sudo su",
        "sudo rm -rf /",
        "shutdown -h now",
        "reboot",
        "poweroff",
        "> /dev/null",
        "2> /dev/null",
        "| sh",
        "| bash",
        "eval 'ls'",
        "exec 'ls'",
        "mkfs /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "fdisk /dev/sda",
        "parted /dev/sda",
    ])
    def test_dangerous_commands_are_blocked(self, interceptor, dangerous_cmd):
        """All dangerous commands should be blocked."""
        safe, reason = interceptor.is_safe(dangerous_cmd)
        assert not safe, f"Command '{dangerous_cmd}' should be blocked but was allowed. Reason: {reason}"

    @pytest.mark.parametrize("safe_cmd", [
        "ls -la",
        "cat file.txt",
        "python test.py",
        "git status",
        "pytest tests/",
        "find . -name '*.py'",
        "grep -r 'pattern' .",
        "echo 'hello'",
        "mkdir new_folder",
        "touch file.txt",
        "cp src dest",
        "mv old new",
        "python -m unittest",
    ])
    def test_safe_commands_are_allowed(self, interceptor, safe_cmd):
        """Safe commands should be allowed."""
        safe, reason = interceptor.is_safe(safe_cmd)
        assert safe, f"Command '{safe_cmd}' should be allowed but was blocked. Reason: {reason}"

    def test_empty_command_is_blocked(self, interceptor):
        """Empty or whitespace-only commands should be blocked."""
        safe, reason = interceptor.is_safe("")
        assert not safe
        assert "Empty" in reason

        safe, reason = interceptor.is_safe("   ")
        assert not safe

    def test_case_insensitive_dangerous_patterns(self, interceptor):
        """Dangerous pattern detection should be case-insensitive."""
        dangerous_variants = [
            "Rm -rf /",  # capital R
            "SUDO su",    # capital SUDO
            "ShUtDoWn",   # mixed case
        ]
        for cmd in dangerous_variants:
            safe, reason = interceptor.is_safe(cmd)
            assert not safe, f"Case variant '{cmd}' should be blocked"

    def test_path_validation_within_allowed_directory(self, interceptor):
        """Paths within allowed directory should be valid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowed_base = Path(tmpdir)
            safe_path = allowed_base / "subdir" / "file.txt"
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            safe_path.touch()

            is_valid, reason = interceptor.validate_path(str(safe_path), allowed_base)
            assert is_valid, f"Path should be valid: {reason}"

    def test_path_validation_escape_attempts(self, interceptor):
        """Path traversal attempts should be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowed_base = Path(tmpdir)

            # Attempts to escape using ..
            escape_paths = [
                "../../../etc/passwd",
                "subdir/../../etc",
                "..",
            ]
            for path in escape_paths:
                is_valid, reason = interceptor.validate_path(path, allowed_base)
                assert not is_valid, f"Escape path '{path}' should be rejected but was allowed"

    def test_path_validation_absolute_paths(self, interceptor):
        """Absolute paths outside allowed directory should be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowed_base = Path(tmpdir)

            # Absolute path to /etc (definitely outside allowed_base)
            absolute_path = "/etc/passwd"
            is_valid, reason = interceptor.validate_path(absolute_path, allowed_base)
            assert not is_valid, "Absolute path outside allowed base should be rejected"

    def test_path_validation_empty_path(self, interceptor):
        """Empty path should be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowed_base = Path(tmpdir)
            is_valid, reason = interceptor.validate_path("", allowed_base)
            assert not is_valid
            assert "Empty" in reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
