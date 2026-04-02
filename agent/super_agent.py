"""
agent/super_agent.py - Native SuperAgent Core for RepoForge

This is the core agent engine that powers the RepoForge system.
Unlike s_full.py which runs tools on the host, this version BONDS all tools
to the Docker Sandbox for secure, isolated execution.

[V2.0 Phase 1 Update]: Added Advanced Tools (get_file_tree, search_codebase, apply_patch)
"""

import json
import os
import base64
import logging
import time
import re
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from openai import OpenAI, APIConnectionError, APITimeoutError, RateLimitError
from dotenv import load_dotenv

load_dotenv(override=True)

from config import get_config
from utils.retry import retry_network
from mcp_client import get_mcp_client, MCPClient
from utils.logging_config import setup_logging
import structlog

config = get_config()

# Initialize structured logging
setup_logging(level=config.log_level)

logger = structlog.get_logger(__name__)

MODEL = config.model_id
API_BASE = config.openai_base_url
API_KEY = config.openai_api_key

client = OpenAI(
    api_key=API_KEY,
    base_url=API_BASE,
    timeout=120.0,
    max_retries=0  # 禁用 SDK 内置重试，使用我们的自定义重试逻辑
)

MAX_ITERATIONS = 20  # 为复杂项目适当增加迭代次数
MAX_TOKENS_PER_RESPONSE = 4096
API_RETRY_DELAY = 10


def _parse_mcp_result(result_str: str) -> Dict[str, Any]:
    """
    Parse MCP tool string output into dictionary format expected by SandboxTools.

    MCP tools return formatted strings like:
        STDOUT:\n...
        STDERR:\n...
        EXIT CODE: <code>
        ERROR: <msg>  (only on errors)

    Returns:
        Dict with keys: stdout, stderr, exit_code, error (optional)
    """
    lines = result_str.splitlines()
    result = {"stdout": "", "stderr": "", "exit_code": 0}

    current_section = None
    for line in lines:
        if line.startswith("STDOUT:"):
            current_section = "stdout"
            result["stdout"] = line[7:].lstrip() + "\n"
        elif line.startswith("STDERR:"):
            current_section = "stderr"
            result["stderr"] = line[7:].lstrip() + "\n"
        elif line.startswith("EXIT CODE:"):
            try:
                result["exit_code"] = int(line[10:].strip())
            except ValueError:
                result["exit_code"] = 1
        elif line.startswith("ERROR:"):
            result["error"] = line[6:].strip()
        else:
            # Continue appending to current section
            if current_section:
                result[current_section] += line + "\n"

    # Trim trailing newlines
    result["stdout"] = result["stdout"].rstrip("\n")
    result["stderr"] = result["stderr"].rstrip("\n")

    return result


@dataclass
class SandboxTools:
    """Container for sandbox-bound tool handlers."""
    task_id: int
    mcp_client: Optional[MCPClient] = None  # Optional injected MCP client

    SANDBOX_PATH = "/workspace"

    def _mcp(self) -> MCPClient:
        """Get the global MCP client or use injected one."""
        if self.mcp_client is not None:
            return self.mcp_client
        return get_mcp_client()

    def _sanitize_path(self, raw_path: str) -> str:
        """
        Convert user-provided path to clean relative path within worktree.
        - Removes Windows drive letters
        - Strips leading slashes
        - Does NOT add /workspace prefix (that's the worktree root)
        """
        clean = raw_path.replace("\\", "/")
        # Remove Windows drive letter (e.g., "D:/" -> "")
        clean = re.sub(r'^[a-zA-Z]:/', '', clean)
        # Remove leading slashes
        clean = clean.lstrip("/")
        # Return clean relative path (no workspace prefix)
        return clean

    def bash(self, command: str) -> str:
        safe_cmd = f"cd {self.SANDBOX_PATH} && {command}"
        result_str = self._mcp().bash_execute(self.task_id, safe_cmd)
        result = _parse_mcp_result(result_str)
        if result.get("error"):
            return f"Error: {result['error']}"
        output = result.get("stdout", "") + result.get("stderr", "")
        return output if output else "(no output)"

    # ==========================================
    # 🌟 V2.0 新增: 视野工具 (File Tree & Search)
    # ==========================================
    def get_file_tree(self, path: str = ".", depth: int = 3) -> str:
        """Get directory structure, ignoring .git and pycache"""
        linux_path = self._sanitize_path(path)
        # 使用 find 模拟 tree 命令，确保在 slim 镜像中也能运行
        cmd = f"find {linux_path} -maxdepth {depth} -not -path '*/\\.git/*' -not -path '*/__pycache__/*' | sort"
        result_str = self._mcp().bash_execute(self.task_id, cmd)
        result = _parse_mcp_result(result_str)
        if result.get("exit_code", 0) != 0:
            return f"Error: {result.get('stderr', 'Failed to get file tree')}"
        output = result.get("stdout", "").strip()
        return output if output else "Directory is empty or not found."

    def search_codebase(self, query: str, path: str = ".") -> str:
        """Search for a string or regex in the codebase using grep"""
        linux_path = self._sanitize_path(path)
        safe_query = query.replace("'", "'\\''")  # 转义单引号
        cmd = f"grep -rn --exclude-dir=.git --exclude-dir=__pycache__ '{safe_query}' {linux_path}"
        result_str = self._mcp().bash_execute(self.task_id, cmd)
        result = _parse_mcp_result(result_str)

        if result.get("exit_code", 0) != 0:
            if result.get("exit_code") == 1:
                return "No matches found."
            return f"Error: {result.get('stderr', 'Search failed')}"

        output = result.get("stdout", "").strip()
        lines = output.split('\n')
        # 防止输出过长撑爆上下文
        if len(lines) > 100:
            return '\n'.join(lines[:100]) + f"\n... (truncated {len(lines)-100} more lines)"
        return output

    # ==========================================
    # 🌟 V2.2 新增: AST 级代码库骨架地图提取
    # ==========================================
    def get_repo_map(self, path: str = ".") -> str:
        """Generate an AST-based skeleton of the Python codebase."""
        linux_path = self._sanitize_path(path)

        ast_script = """
import os, ast

def get_map(root):
    res = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [dir for dir in dirs if dir not in ['.git', '__pycache__', 'venv', 'env', 'node_modules']]
        for f in files:
            if not f.endswith('.py'): continue
            fp = os.path.join(d, f)
            try:
                with open(fp, 'r', encoding='utf-8') as file:
                    tree = ast.parse(file.read())
                rel = os.path.relpath(fp, root)
                out = [f"📄 {rel}"]
                for node in tree.body:
                    if isinstance(node, ast.ClassDef):
                        out.append(f"  class {node.name}:")
                        for sub in node.body:
                            if isinstance(sub, ast.FunctionDef):
                                out.append(f"    def {sub.name}(...)")
                    elif isinstance(node, ast.FunctionDef):
                        out.append(f"  def {node.name}(...)")
                if len(out) > 1: res.append('\\n'.join(out))
            except Exception:
                pass
    return '\\n\\n'.join(res) if res else 'No Python classes/functions found.'

print(get_map('.'))
"""
        encoded = base64.b64encode(ast_script.encode()).decode()
        cmd = f"cd {linux_path} && python3 -c \"import base64; exec(base64.b64decode('{encoded}').decode())\""

        result_str = self._mcp().bash_execute(self.task_id, cmd)
        result = _parse_mcp_result(result_str)
        if result.get("exit_code", 0) != 0:
            return f"Error: {result.get('stderr', 'Failed to generate repo map')}"

        output = result.get("stdout", "").strip()
        if len(output) > 8000:
            output = output[:8000] + "\n... (Repo map truncated due to length)"
        return output if output else "No Python structure found."

    # ==========================================
    # 🛠️ 基础文件操作工具
    # ==========================================
    def read_file(self, path: str) -> str:
        linux_path = self._sanitize_path(path)
        result_str = self._mcp().file_read(self.task_id, linux_path)
        # file_read returns plain text or error message, no parsing needed
        return result_str

    def write_file(self, path: str, content: str) -> str:
        linux_path = self._sanitize_path(path)
        result_str = self._mcp().file_write(self.task_id, linux_path, content)
        return result_str

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        linux_path = self._sanitize_path(path)
        # Read current content using file_read (MCP tool returns direct content)
        current_content = self._mcp().file_read(self.task_id, linux_path)
        if current_content.startswith("Error:"):
            return current_content

        # Perform replacement in memory
        if old_text not in current_content:
            return f"Error: old_text not found in {linux_path}"

        new_content = current_content.replace(old_text, new_text, 1)
        result_str = self._mcp().file_write(self.task_id, linux_path, new_content)
        return result_str

    # ==========================================
    # 🌟 V2.0 新增: 高级重构工具 (Apply Patch)
    # ==========================================
    def apply_patch(self, patch_content: str) -> str:
        """Apply a unified diff patch to the repository."""
        patch_file = f"{self.SANDBOX_PATH}/.temp_task_{self.task_id}.patch"
        # Write patch file using MCP file_write
        write_result = self._mcp().file_write(self.task_id, patch_file, patch_content)
        if write_result.startswith("Error:"):
            return f"Failed to write patch file: {write_result}"

        # Apply patch using bash
        cmd = f"cd {self.SANDBOX_PATH} && (patch -p1 < {patch_file} || patch -p0 < {patch_file})"
        result_str = self._mcp().bash_execute(self.task_id, cmd)

        # Clean up temp file (best effort)
        self._mcp().bash_execute(self.task_id, f"rm -f {patch_file}")

        result = _parse_mcp_result(result_str)
        if result.get("exit_code", 0) != 0:
            return f"Error applying patch. Make sure the patch format is correct Unified Diff.\nStdout: {result.get('stdout', '')}\nStderr: {result.get('stderr', '')}"
        return f"Patch applied successfully:\n{result.get('stdout', '')}"


def run_agent_task(
    task_prompt: str,
    task_id: int,
    workspace_path: Path,
    max_iterations: int = MAX_ITERATIONS,
    mcp_client=None
) -> Dict[str, Any]:
    """Run the agent task using MCP client for sandbox operations."""

    tools = SandboxTools(task_id=task_id, mcp_client=mcp_client)  # Use injected client if provided

    TOOL_HANDLERS = {
        "bash": lambda **kw: tools.bash(kw["command"]),
        "get_file_tree": lambda **kw: tools.get_file_tree(kw.get("path", "."), kw.get("depth", 3)),
        "get_repo_map": lambda **kw: tools.get_repo_map(kw.get("path", ".")),
        "search_codebase": lambda **kw: tools.search_codebase(kw["query"], kw.get("path", ".")),
        "read_file": lambda **kw: tools.read_file(kw["path"]),
        "write_file": lambda **kw: tools.write_file(kw["path"], kw["content"]),
        "edit_file": lambda **kw: tools.edit_file(kw["path"], kw["old_text"], kw["new_text"]),
        "apply_patch": lambda **kw: tools.apply_patch(kw["patch_content"]),
    }

    TOOL_SCHEMAS = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Execute a bash command in /workspace. Use this to run tests (e.g., pytest, python main.py).",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_file_tree",
                "description": "Get the directory structure of the project. ALWAYS use this first to understand the project layout.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Target directory (default: '.')"},
                        "depth": {"type": "integer", "description": "Depth of tree (default: 3)"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_repo_map",
                "description": "Generate an AST-based skeleton of all Python classes and functions in the project. Use this immediately after get_file_tree to understand the architecture.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Target directory (default: '.')"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_codebase",
                "description": "Search for specific keywords, function names, or variables across the entire codebase.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The string or regex to search for."},
                        "path": {"type": "string", "description": "Directory to search in (default: '.')"}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the entire contents of a specific file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create a new file or completely overwrite an existing one.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"}
                    },
                    "required": ["path", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace a specific small section of text in a file. Suitable for 1-2 line changes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string", "description": "Exact text to replace."},
                        "new_text": {"type": "string", "description": "New text to insert."}
                    },
                    "required": ["path", "old_text", "new_text"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": "Apply a Unified Diff patch. Best for complex, multi-line refactoring across one or multiple files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patch_content": {
                            "type": "string", 
                            "description": "Standard Unified Diff string. E.g., \n--- a/file.py\n+++ b/file.py\n@@ -1,3 +1,4 @@\n..."
                        }
                    },
                    "required": ["patch_content"]
                }
            }
        },
    ]

# 【核心修复】：将 Coder 降级为严格的执行者
    SYSTEM_PROMPT = """You are SuperAgent, an elite Silicon Valley software engineer.
Your working directory is '/workspace'.

WORKFLOW RULES:
1. READ THE PLAN: The Software Architect has already analyzed the project and written a plan. Your VERY FIRST step MUST be to use `read_file` to read `/workspace/task_plan.md`.
2. EXECUTE: Strictly follow the step-by-step instructions in `task_plan.md`.
3. MODIFY: Use `edit_file`, `write_file`, or `apply_patch` to make the exact changes requested in the plan.
4. VERIFY: ALWAYS test your changes using `bash` (e.g., `python -m unittest`) to ensure they work.

Do NOT explain what you are going to do in plain text. Execute the tool calls immediately based on the plan.
When you have successfully tested everything, output a final summary message."""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task_prompt}
    ]

    iterations = 0
    last_content = ""

    try:
        while iterations < max_iterations:
            iterations += 1
            logger.info(f"=== Iteration {iterations}/{max_iterations} ===")

            try:
                # 🌟 使用重试装饰器自动处理网络错误
                response = _call_openai_with_retry(
                    model=MODEL,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    max_tokens=MAX_TOKENS_PER_RESPONSE,
                    temperature=0
                )
            except Exception as api_error:
                error_str = str(api_error)
                logger.error(f"❌ [API ERROR] {error_str}")
                return {"success": False, "final_message": f"API error: {error_str}", "iterations": iterations, "git_diff": ""}

            assistant_msg = response.choices[0].message
            
            safe_msg = {
                "role": "assistant",
                "content": assistant_msg.content or ""
            }
            if assistant_msg.tool_calls:
                safe_msg["tool_calls"] = [
                    {
                        "id": t.id,
                        "type": "function",
                        "function": {"name": t.function.name, "arguments": t.function.arguments}
                    } for t in assistant_msg.tool_calls
                ]
            messages.append(safe_msg)

            if not assistant_msg.tool_calls:
                last_content = assistant_msg.content or ""
                logger.info(f"📝 [FINISH] {last_content[:100]}...")
                break

            for tool_call in assistant_msg.tool_calls:
                tool_name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                    logger.info(f"🛠️ [TOOL] {tool_name} | args: {str(args)[:100]}")
                except Exception:
                    args = {}

                handler = TOOL_HANDLERS.get(tool_name)
                if not handler:
                    output = f"Unknown tool: {tool_name}"
                else:
                    try:
                        output = str(handler(**args))
                    except Exception as tool_error:
                        output = f"Error: {tool_error}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": output[:5000]
                })

            time.sleep(2) 

    except Exception as e:
        return {"success": False, "final_message": str(e), "iterations": iterations, "git_diff": ""}

    return {"success": True, "final_message": last_content, "iterations": iterations, "git_diff": ""}


def extract_git_diff(workspace_path: Path) -> str:
    """Extract the git diff robustly from the HOST machine."""
    import shutil

    try:
        logger.info(f"Extracting diff from host path: {workspace_path}")

        # 💥 V2.1 核心修复：跨平台物理净化！在 diff 前强制删除所有 Python 编译缓存
        for pycache_dir in workspace_path.rglob("__pycache__"):
            shutil.rmtree(pycache_dir, ignore_errors=True)
        for pyc_file in workspace_path.rglob("*.pyc"):
            pyc_file.unlink(missing_ok=True)

        # 💥 V2.2 核心修复：物理删除 Planner 生成的图纸和 patch 残留，防止被 Git 强行收录
        plan_file = workspace_path / "task_plan.md"
        if plan_file.exists():
            plan_file.unlink(missing_ok=True)
        for patch_file in workspace_path.glob(".temp_task_*.patch"):
            patch_file.unlink(missing_ok=True)

        # 1. 强制追踪所有新文件
        subprocess.run(["git", "add", "-N", "."], cwd=workspace_path, capture_output=True)

        # 2. 提取标准 diff
        res1 = subprocess.run(["git", "diff"], cwd=workspace_path, capture_output=True, text=True, encoding='utf-8')
        if res1.stdout and res1.stdout.strip():
            return res1.stdout.strip()

        # 3. 容错：如果 Agent 自行 Commit 了，比较上一个版本
        res2 = subprocess.run(["git", "diff", "HEAD~1", "HEAD"], cwd=workspace_path, capture_output=True, text=True, encoding='utf-8')
        if res2.stdout and res2.stdout.strip():
            return f"[Agent 已在沙盒中自主 Commit]\n{res2.stdout.strip()}"

        return "(no changes detected)"
    except Exception as e:
        logger.error(f"Host git diff extraction failed: {e}")
        return f"(git diff failed: {e})"


# 🌟 封装 OpenAI API 调用，应用重试装饰器
@retry_network(
    max_attempts=3,
    base_delay=2.0,
    on_retry=lambda attempt, exc, delay: logger.warning(
        f"OpenAI API retry {attempt}: {exc}. Waiting {delay:.1f}s"
    )
)
def _call_openai_with_retry(
    model: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict],
    max_tokens: int,
    temperature: float = 0
) -> Any:
    """
    Call OpenAI API with automatic retry logic.

    This function is decorated with @retry_network to handle:
    - Connection errors
    - Timeout errors
    - Rate limiting (429)
    - Network issues

    Args:
        model: Model identifier
        messages: Chat messages
        tools: Function schemas
        max_tokens: Max tokens for response
        temperature: Sampling temperature

    Returns:
        OpenAI response object
    """
    return client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature
    )