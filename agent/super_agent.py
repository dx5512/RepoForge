"""
agent/super_agent.py - Native SuperAgent Core for Auto-SWE-Deer

This is the core agent engine that powers the Auto-SWE-Deer system.
Unlike s_full.py which runs tools on the host, this version BONDS all tools
to the Docker Sandbox for secure, isolated execution.

Key Design:
- All tools (bash, read, write, edit) route to SandboxController
- Task execution happens inside isolated Docker containers
- Git worktrees provide directory-level isolation
"""

import json
import os
import base64
import logging
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

MODEL = os.getenv("OPENAI_MODEL_NAME", "step-3.5-flash")
API_BASE = os.getenv("OPENAI_BASE_URL", "https://api.stepfun.com/v1")
API_KEY = os.getenv("OPENAI_API_KEY", "")

client = OpenAI(
    api_key=API_KEY,
    base_url=API_BASE,
    timeout=120.0,
    max_retries=0
)

MAX_ITERATIONS = 30
MAX_TOKENS_PER_RESPONSE = 4096
API_RETRY_DELAY = 10


@dataclass
class SandboxTools:
    """Container for sandbox-bound tool handlers."""
    execute_func: callable
    task_id: int

    SANDBOX_PATH = "/workspace"

    def bash(self, command: str) -> str:
        result = self.execute_func(command)
        if result.get("error"):
            return f"Error: {result['error']}"
        output = result.get("stdout", "") + result.get("stderr", "")
        return output if output else "(no output)"

    def read_file(self, path: str) -> str:
        clean_path = path.lstrip("/")
        if not clean_path.startswith(self.SANDBOX_PATH):
            linux_path = f"{self.SANDBOX_PATH}/{clean_path}"
        else:
            linux_path = clean_path

        cmd = f"cat {linux_path}"
        result = self.execute_func(cmd)
        if result.get("exit_code", 0) != 0:
            return f"Error: {result.get('stderr', 'File not found or cannot read')}"
        return result.get("stdout", "")

    def write_file(self, path: str, content: str) -> str:
        clean_path = path.lstrip("/")
        if not clean_path.startswith(self.SANDBOX_PATH):
            linux_path = f"{self.SANDBOX_PATH}/{clean_path}"
        else:
            linux_path = clean_path

        encoded = base64.b64encode(content.encode()).decode()
        cmd = f"echo {encoded} | base64 -d > {linux_path}"
        result = self.execute_func(cmd)
        if result.get("exit_code", 0) != 0:
            return f"Error: {result.get('stderr', 'Write failed')}"
        return f"Wrote {len(content)} bytes to {path}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        clean_path = path.lstrip("/")
        if not clean_path.startswith(self.SANDBOX_PATH):
            linux_path = f"{self.SANDBOX_PATH}/{clean_path}"
        else:
            linux_path = clean_path

        old_encoded = base64.b64encode(old_text.encode()).decode()
        new_encoded = base64.b64encode(new_text.encode()).decode()

        python_script = f"import base64,sys; c=open('{linux_path}').read(); c=c.replace(base64.b64decode('{old_encoded}').decode(),base64.b64decode('{new_encoded}').decode(),1); open('{linux_path}','w').write(c)"
        cmd = f"python3 -c \"{python_script}\""

        result = self.execute_func(cmd)
        if result.get("exit_code", 0) != 0:
            return f"Error: {result.get('stderr', 'Edit failed')}"
        return f"Edited {path}"

    def file_exists(self, path: str) -> bool:
        clean_path = path.lstrip("/")
        if not clean_path.startswith(self.SANDBOX_PATH):
            linux_path = f"{self.SANDBOX_PATH}/{clean_path}"
        else:
            linux_path = clean_path

        result = self.execute_func(f"test -f {linux_path} && echo exists")
        return "exists" in result.get("stdout", "")


def run_agent_task(
    task_prompt: str,
    sandbox_controller,
    workspace_path: Path,
    max_iterations: int = MAX_ITERATIONS
) -> Dict[str, Any]:
    """
    Run the native agent loop with sandbox-bound tools.

    Args:
        task_prompt: The user's task/objective
        sandbox_controller: SandboxController instance for tool execution
        workspace_path: Path to the sandbox workspace (host path)
        max_iterations: Max LLM calls to prevent infinite loops

    Returns:
        Dict with keys: success, final_message, iterations, git_diff
    """
    tools = SandboxTools(
        execute_func=sandbox_controller.execute_in_sandbox,
        task_id=sandbox_controller.task_id
    )

    TOOL_HANDLERS = {
        "bash": lambda **kw: tools.bash(kw["command"]),
        "read_file": lambda **kw: tools.read_file(kw["path"]),
        "write_file": lambda **kw: tools.write_file(kw["path"], kw["content"]),
        "edit_file": lambda **kw: tools.edit_file(kw["path"], kw["old_text"], kw["new_text"]),
    }

    TOOL_SCHEMAS = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Execute a bash command inside the sandbox. Use this for git, python, ls, cat, etc.",
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
                "name": "read_file",
                "description": "Read the entire contents of a file.",
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
                "description": "Overwrite a file with new content.",
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
                "description": "Replace a specific section of a file. Use this for targeted fixes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"}
                    },
                    "required": ["path", "old_text", "new_text"]
                }
            }
        },
    ]

    SYSTEM_PROMPT = f"""You are SuperAgent, an elite software engineering AI.

CRITICAL: You are executing inside a secure Linux Docker Sandbox.
The repository is mounted at '/workspace' (NOT a Windows path like D:\\...).

Your workspace: /workspace
Your commands (bash, git, python) MUST use Linux paths, NOT Windows paths.

Available tools (all execute inside the sandbox):
- bash: Run shell commands (git, python, ls, cat, etc.)
- read_file: Read entire file contents (use Linux paths like /workspace/hello.py)
- write_file: Overwrite a file completely
- edit_file: Replace specific text in a file

CRITICAL INSTRUCTIONS:
1. Start by exploring: bash("cd /workspace && ls -la")
2. Read files using Linux paths: read_file("/workspace/hello.py")
3. Edit files using Linux paths: edit_file("/workspace/hello.py", ...)
4. All file operations are relative to /workspace inside the sandbox
5. After fixing code, test with: bash("cd /workspace && python hello.py")
6. When done, the git diff will be automatically extracted

Do NOT use Windows paths (D:\\, C:\\, etc.) in any command!"""

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
                logger.info(f"🛠️ [API CALL] Sending request to LLM...")
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    max_tokens=MAX_TOKENS_PER_RESPONSE,
                    temperature=0
                )
                logger.info(f"📨 [API RESPONSE] Received response")
            except Exception as api_error:
                error_str = str(api_error)
                logger.error(f"❌ [API ERROR] {error_str}")

                if "429" in error_str or "Too Many Requests" in error_str:
                    logger.warning(f"⏳ [RATE LIMIT] Got 429, waiting {API_RETRY_DELAY}s before retry...")
                    time.sleep(API_RETRY_DELAY)
                    continue
                else:
                    return {
                        "success": False,
                        "final_message": f"API error: {error_str}",
                        "iterations": iterations,
                        "git_diff": ""
                    }

            assistant_msg = response.choices[0].message
            messages.append(assistant_msg.model_dump(exclude_none=True))

            if not assistant_msg.tool_calls:
                last_content = assistant_msg.content or ""
                logger.info(f"📝 [NO TOOLS] Final response: {last_content[:200]}...")
                break

            logger.info(f"🔧 [TOOL CALLS] {len(assistant_msg.tool_calls)} tool(s) called")

            for tool_call in assistant_msg.tool_calls:
                tool_name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                    logger.info(f"🛠️ [TOOL CALL] {tool_name} with args: {str(args)[:200]}")
                except json.JSONDecodeError as e:
                    logger.error(f"❌ [TOOL ARGS ERROR] Failed to parse args: {e}")
                    args = {}

                handler = TOOL_HANDLERS.get(tool_name)
                if not handler:
                    output = f"Unknown tool: {tool_name}"
                    logger.warning(f"⚠️ [UNKNOWN TOOL] {tool_name}")
                else:
                    try:
                        output = str(handler(**args))
                        logger.info(f"📄 [TOOL RESULT] {output[:500]}...")
                    except Exception as tool_error:
                        output = f"Error executing {tool_name}: {tool_error}"
                        logger.error(f"❌ [TOOL ERROR] {output}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": output[:10000]
                })

            last_content = ""
            time.sleep(2)

    except Exception as e:
        logger.error(f"❌ [LOOP ERROR] {e}")
        return {
            "success": False,
            "final_message": f"Agent error: {e}",
            "iterations": iterations,
            "git_diff": ""
        }

    logger.info(f"✅ [COMPLETE] Agent finished after {iterations} iterations")
    return {
        "success": True,
        "final_message": last_content,
        "iterations": iterations,
        "git_diff": ""
    }


def extract_git_diff(sandbox_controller) -> str:
    """Extract the git diff from the sandbox workspace."""
    result = sandbox_controller.execute_in_sandbox("git diff HEAD -- .")
    if result.get("stdout"):
        return result["stdout"]

    result = sandbox_controller.execute_in_sandbox("git status --short")
    if result.get("stdout"):
        return f"Git status:\n{result['stdout']}"

    return "(no changes detected)"
