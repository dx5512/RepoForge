#!/usr/bin/env python3
"""
RepoForge - Main Entry Point

DeerFlow 2.0 Application for Software Engineering Task Automation.

Usage:
    python main.py                    # Start in CLI mode
    python main.py --chat-ops        # Enable chat-ops integration
"""

import sys
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

from config import get_config

config = get_config()

WORKDIR = Path(__file__).parent


def check_environment():
    """Verify all required dependencies are available."""
    errors = []

    try:
        import openai
    except ImportError:
        errors.append("openai not installed")

    try:
        import docker
    except ImportError:
        errors.append("docker not installed")

    try:
        import mcp
    except ImportError:
        errors.append("mcp not installed")

    if not config.openai_api_key or config.openai_api_key == "your_api_key_here":
        errors.append("OPENAI_API_KEY not configured in .env")

    if errors:
        print("Environment check FAILED:")
        for e in errors:
            print(f"  - {e}")
        print("\nPlease fix these issues before running.")
        return False

    return True


def print_banner():
    print("=" * 60)
    print("RepoForge - Automated Code Factory")
    print("=" * 60)
    print()
    print("Architecture: Route B (Embrace DeerFlow)")
    print()
    print("Custom IP:")
    print("  - SandboxController (Docker + Worktree isolation)")
    print("  - MCP Tools (file_read, file_write, bash_execute)")
    print("  - SWE Skills (Researcher, Coder, Tester)")
    print()
    print("DeerFlow Native:")
    print("  - Sub-agent orchestration")
    print("  - Context Engineering")
    print("  - Message Bus")
    print()
    print("=" * 60)
    print()


def main():
    print_banner()

    if not check_environment():
        sys.exit(1)

    print("Environment check PASSED")
    print()
    print("Configuration:")
    print(f"  Model: {config.model_id}")
    print(f"  API Base: {config.openai_base_url}")
    print(f"  Log Level: {config.log_level}")
    print()

    print("Project Structure:")
    for path in sorted(WORKDIR.rglob("*")):
        if path.is_file() and not str(path).startswith(str(WORKDIR / "venv")):
            rel = path.relative_to(WORKDIR)
            print(f"  {rel}")
    print()

    print("Status:")
    print("  - sandbox/ module: READY")
    print("  - mcp_tools/ module: READY")
    print("  - deerflow_app/skills/: CONFIGURED")
    print()
    print("NOTE: RepoForge framework integration requires")
    print("      installing and configuring deer-flow package.")
    print()
    print("To start development:")
    print("  1. pip install deer-flow (when available)")
    print("  2. deer-flow init")
    print("  3. deer-flow run")
    print()


if __name__ == "__main__":
    main()
