"""
CommandInterceptor - Dangerous Command Blocking

Prevents execution of dangerous commands like:
- rm -rf / (recursive force delete)
- sudo (privilege escalation)
- shutdown/reboot (system control)
- Direct /dev/null redirection (hidden output)
"""

import re
from pathlib import Path


class CommandInterceptor:
    """
    Intercepts and blocks dangerous shell commands.

    SECURITY: This is a critical security layer - always check commands
    before execution in sandbox.
    """

    DANGEROUS_PATTERNS = [
        r"rm\s+-rf\s+/",           # rm -rf /
        r"rm\s+-rf\s+\*",           # rm -rf *
        r"rm\s+-rf\s+~",           # rm -rf ~
        r"sudo\s+",                 # privilege escalation
        r"shutdown",                 # system shutdown
        r"reboot",                   # system reboot
        r"poweroff",                 # system poweroff
        r">\s*/dev/null",           # hide output
        r"2>\s*/dev/null",          # hide errors
        r"\|\s*sh\b",               # pipe to shell
        r"\|\s*bash\b",             # pipe to bash
        r"eval\s+",                  # eval execution
        r"exec\s+",                  # exec replacement
    ]

    DANGEROUS_KEYWORDS = [
        "mkfs",                      # filesystem creation
        "dd",                        # direct disk operation
        "fdisk",                     # disk partitioning
        "parted",                    # disk partitioning
        "chmod",                     # permission changes (specific dangerous ones)
        "chown",                     # ownership changes
    ]

    def __init__(self):
        self.patterns = [re.compile(p, re.IGNORECASE) for p in self.DANGEROUS_PATTERNS]

    def is_safe(self, command: str) -> tuple[bool, str]:
        """
        Check if a command is safe to execute.

        Returns (is_safe, reason) tuple.
        """
        if not command or not command.strip():
            return False, "Empty command"

        cmd_lower = command.lower()

        for pattern in self.patterns:
            if pattern.search(command):
                return False, f"Dangerous pattern matched: {pattern.pattern}"

        for keyword in self.DANGEROUS_KEYWORDS:
            if keyword in cmd_lower:
                # Check if keyword appears as a standalone word (not part of another word)
                parts = cmd_lower.split()
                if keyword in parts:
                    # Always block these dangerous keywords regardless of position
                    return False, f"Dangerous keyword: {keyword}"

        return True, "OK"

    def validate_path(self, path: str, allowed_base: Path) -> tuple[bool, str]:
        """
        Validate that a path is within allowed base directory.

        Prevents path traversal attacks.

        Returns (is_valid, reason) tuple.
        """
        if not path:
            return False, "Empty path"

        try:
            resolved = (allowed_base / path).resolve()
            if not resolved.is_relative_to(allowed_base.resolve()):
                return False, f"Path escapes allowed directory: {path}"
            return True, "OK"
        except Exception as e:
            return False, f"Path validation error: {e}"
