"""
Sandbox Module - Custom IP for Auto-SWE-Deer

This module provides:
- SandboxController: Docker container lifecycle management with proper permission handling
- CommandInterceptor: Dangerous command blocking
- TaskRegistry: task_id -> SandboxController instance mapping
"""

from .container import SandboxController
from .interceptors import CommandInterceptor
from .registry import TaskRegistry

__all__ = ["SandboxController", "CommandInterceptor", "TaskRegistry"]
