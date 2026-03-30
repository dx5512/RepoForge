"""
agent/ - Native Agent Core for Auto-SWE-Deer

This module contains the native SuperAgent implementation that powers
the Auto-SWE-Deer system with sandbox-bound tools.
"""

from .super_agent import run_agent_task, extract_git_diff, SandboxTools

__all__ = ["run_agent_task", "extract_git_diff", "SandboxTools"]
