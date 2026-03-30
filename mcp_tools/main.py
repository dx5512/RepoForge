"""
MCP Tools - Sandboxed File/Bash Operations (MCP SDK Implementation)

This MCP server provides tools that route operations through the SandboxController
based on task_id (State Handoff solution).

Uses MCP SDK's stdio_server for standard MCP protocol compliance.
"""

import sys
import os
import asyncio
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from sandbox import TaskRegistry, SandboxController
from sandbox.interceptors import CommandInterceptor

WORKDIR = Path.cwd()
SANDBOX_BASE = WORKDIR / ".worktrees"
SANDBOX_BASE.mkdir(exist_ok=True)

INTERCEPTOR = CommandInterceptor()

_task_registry: TaskRegistry = None


def get_registry() -> TaskRegistry:
    global _task_registry
    if _task_registry is None:
        _task_registry = TaskRegistry(SANDBOX_BASE)
    return _task_registry


def get_sandbox_for_task(task_id: int) -> tuple[SandboxController, str]:
    """Get sandbox for task_id or return error."""
    registry = get_registry()
    sandbox = registry.get(task_id)

    if sandbox is None:
        return None, f"No sandbox found for task_id={task_id}. Create worktree first."
    if not sandbox.is_running():
        return None, f"Sandbox for task_id={task_id} is not running"
    return sandbox, None


async def file_read(task_id: int, path: str, limit: int = None) -> TextContent:
    """Read file from sandbox worktree."""
    sandbox, error = get_sandbox_for_task(task_id)
    if error:
        return TextContent(type="text", text=f"Error: {error}")

    safe_path = sandbox.worktree_path / path
    if not safe_path.exists():
        return TextContent(type="text", text=f"Error: File not found: {path}")

    try:
        content = safe_path.read_text(encoding="utf-8")
        if limit:
            lines = content.splitlines()
            content = "\n".join(lines[:limit])
        return TextContent(type="text", text=content)
    except Exception as e:
        return TextContent(type="text", text=f"Error: {e}")


async def file_write(task_id: int, path: str, content: str) -> TextContent:
    """Write content to file in sandbox worktree."""
    sandbox, error = get_sandbox_for_task(task_id)
    if error:
        return TextContent(type="text", text=f"Error: {error}")

    safe_path = sandbox.worktree_path / path

    try:
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(content, encoding="utf-8")
        return TextContent(type="text", text=f"OK: Wrote {len(content)} bytes to {path}")
    except Exception as e:
        return TextContent(type="text", text=f"Error: {e}")


async def bash_execute(task_id: int, command: str, timeout: int = 120) -> TextContent:
    """Execute command in sandbox."""
    sandbox, error = get_sandbox_for_task(task_id)
    if error:
        return TextContent(type="text", text=f"Error: {error}")

    safe, reason = INTERCEPTOR.is_safe(command)
    if not safe:
        return TextContent(type="text", text=f"Error: Blocked: {reason}")

    result = sandbox.execute_in_sandbox(command, timeout)

    output = []
    if result["stdout"]:
        output.append(f"STDOUT:\n{result['stdout']}")
    if result["stderr"]:
        output.append(f"STDERR:\n{result['stderr']}")
    output.append(f"EXIT CODE: {result['exit_code']}")
    if result.get("error"):
        output.append(f"ERROR: {result['error']}")

    return TextContent(type="text", text="\n".join(output))


async def worktree_create(task_id: int, name: str, base_ref: str = "HEAD") -> TextContent:
    """Create a worktree and sandbox for a task."""
    from sandbox.worktree import WorktreeManager

    registry = get_registry()

    if registry.get(task_id):
        return TextContent(type="text", text=f"Error: Sandbox already exists for task_id={task_id}")

    wtm = WorktreeManager(WORKDIR, SANDBOX_BASE)

    try:
        wt_info = wtm.create(name, task_id, base_ref)
        wt_path = Path(wt_info["path"])

        sandbox = SandboxController()
        sandbox.create_container(task_id, wt_path)
        registry.register(task_id, sandbox)

        return TextContent(type="text", text=f"OK: Created worktree '{name}' for task {task_id}")
    except Exception as e:
        return TextContent(type="text", text=f"Error: {e}")


async def worktree_list() -> TextContent:
    """List all worktrees."""
    from sandbox.worktree import WorktreeManager
    wtm = WorktreeManager(WORKDIR, SANDBOX_BASE)
    worktrees = wtm.list_all()

    if not worktrees:
        return TextContent(type="text", text="No worktrees")

    lines = []
    for wt in worktrees:
        lines.append(f"- {wt['name']}: {wt['path']} (task={wt.get('task_id')}, status={wt.get('status')})")
    return TextContent(type="text", text="\n".join(lines))


async def worktree_remove(task_id: int, name: str = None, keep_files: bool = False) -> TextContent:
    """Remove worktree and sandbox for a task."""
    from sandbox.worktree import WorktreeManager

    registry = get_registry()
    wtm = WorktreeManager(WORKDIR, SANDBOX_BASE)

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
            return TextContent(type="text", text=f"Warning: {e}")

    return TextContent(type="text", text=f"OK: Removed sandbox for task {task_id}")


TOOLS = [
    Tool(
        name="file_read",
        description="Read file from sandbox worktree",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task identifier for routing"},
                "path": {"type": "string", "description": "Relative path within worktree"},
                "limit": {"type": "integer", "description": "Optional line limit"}
            },
            "required": ["task_id", "path"]
        }
    ),
    Tool(
        name="file_write",
        description="Write content to file in sandbox worktree",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task identifier for routing"},
                "path": {"type": "string", "description": "Relative path within worktree"},
                "content": {"type": "string", "description": "Content to write"}
            },
            "required": ["task_id", "path", "content"]
        }
    ),
    Tool(
        name="bash_execute",
        description="Execute command in sandbox",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task identifier for routing"},
                "command": {"type": "string", "description": "Command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"}
            },
            "required": ["task_id", "command"]
        }
    ),
    Tool(
        name="worktree_create",
        description="Create a worktree and sandbox for a task",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task identifier"},
                "name": {"type": "string", "description": "Worktree name"},
                "base_ref": {"type": "string", "description": "Git reference (default: HEAD)"}
            },
            "required": ["task_id", "name"]
        }
    ),
    Tool(
        name="worktree_list",
        description="List all worktrees",
        inputSchema={"type": "object", "properties": {}}
    ),
    Tool(
        name="worktree_remove",
        description="Remove worktree and sandbox for a task",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task identifier"},
                "name": {"type": "string", "description": "Worktree name (optional)"},
                "keep_files": {"type": "boolean", "description": "If True, keep worktree files"}
            },
            "required": ["task_id"]
        }
    ),
]


async def handle_tool_call(name: str, arguments: dict) -> list[TextContent]:
    """Route tool call to appropriate handler."""
    task_id = arguments.get("task_id")

    handlers = {
        "file_read": lambda: file_read(task_id, arguments["path"], arguments.get("limit")),
        "file_write": lambda: file_write(task_id, arguments["path"], arguments["content"]),
        "bash_execute": lambda: bash_execute(task_id, arguments["command"], arguments.get("timeout", 120)),
        "worktree_create": lambda: worktree_create(task_id, arguments["name"], arguments.get("base_ref", "HEAD")),
        "worktree_list": lambda: worktree_list(),
        "worktree_remove": lambda: worktree_remove(task_id, arguments.get("name"), arguments.get("keep_files", False)),
    }

    handler = handlers.get(name)
    if handler:
        result = await handler()
        return [result]
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    server = Server("sandbox-tools")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        return await handle_tool_call(name, arguments)

    options = stdio_server.ServerOptions(
        capabilities={}
    )

    async with stdio_server.stdio_server(options) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            options
        )


if __name__ == "__main__":
    asyncio.run(main())
