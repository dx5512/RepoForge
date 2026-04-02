"""
agent/planner_agent.py - Software Architect / Planner Agent

Explores the codebase and creates a detailed task_plan.md for the SuperAgent (Coder) to execute.
"""

import json
import logging
import time
from typing import Dict, Any

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger(__name__)

from config import get_config
from utils.retry import retry_network
from utils.logging_config import setup_logging
import structlog

config = get_config()

# Initialize structured logging
setup_logging(level=config.log_level)

logger = structlog.get_logger(__name__)

# 复用 SuperAgent 的模型配置
from agent.super_agent import SandboxTools, MAX_TOKENS_PER_RESPONSE

# 创建独立的 OpenAI 客户端（Planner 使用自己的配置）
client = OpenAI(
    api_key=config.openai_api_key,
    base_url=config.openai_base_url,
    timeout=120.0,
    max_retries=0
)

MODEL = config.model_id

# 封装 API 调用
@retry_network(max_attempts=3, base_delay=2.0)
def _call_openai_with_retry(model: str, messages: list, tools: list, max_tokens: int, temperature: float = 0):
    return client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature
    )

def run_planner_task(
    task_prompt: str,
    task_id: int,
    max_iterations: int = 12,  # Increased to give planner more time
    mcp_client=None
) -> Dict[str, Any]:
    """
    运行规划者智能体，产出 task_plan.md

    Args:
        task_prompt: User's request
        task_id: Task ID for sandbox operations
        max_iterations: Maximum LLM iterations
        mcp_client: Optional MCPClient instance (for custom worktrees_base)
    """
    tools = SandboxTools(task_id=task_id, mcp_client=mcp_client)

    # ⚠️ 架构师的工具箱被严格限制：没有 edit_file, 没有 apply_patch
    TOOL_HANDLERS = {
        "get_file_tree": lambda **kw: tools.get_file_tree(kw.get("path", "."), kw.get("depth", 3)),
        "get_repo_map": lambda **kw: tools.get_repo_map(kw.get("path", ".")),
        "search_codebase": lambda **kw: tools.search_codebase(kw["query"], kw.get("path", ".")),
        "read_file": lambda **kw: tools.read_file(kw["path"]),
        "write_file": lambda **kw: tools.write_file(kw["path"], kw["content"]),
    }

    TOOL_SCHEMAS = [
        {
            "type": "function",
            "function": {
                "name": "get_file_tree",
                "description": "Get directory structure. ALWAYS use this first.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_repo_map",
                "description": "Generate an AST-based skeleton of all Python classes and functions in the project. Use this immediately after get_file_tree to understand the architecture.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_codebase",
                "description": "Search for keywords or functions.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}}, "required": ["query"]}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read file contents to understand context.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write the final task_plan.md file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}
            }
        }
    ]

    SYSTEM_PROMPT = """You are an elite Software Architect. Your sole purpose is to analyze the user's request and produce a detailed execution plan in 'task_plan.md'. You have a maximum of 6 tool calls.

WORKFLOW (strictly follow this sequence):

1. [1 call] get_file_tree(path='.', depth=2) - get the folder layout.
2. [1 call] get_repo_map(path='.') - obtain the AST skeleton of the Python project.
3. [2-3 calls] Based on the repo map, identify the file(s) and function(s) to modify. Use search_codebase (at most 2 times) and read_file (at most 2 times) to inspect the relevant code.
4. [1 call] write_file(path='task_plan.md', content=your_plan) - this is your FINAL action.

CRITICAL RULES:
- You MUST call write_file before you run out of iterations. Do not exceed 6 total tool calls.
- After you have read the necessary files, you MUST call write_file immediately. Do NOT make any more tool calls.
- The plan must be comprehensive and include file paths and exact changes.
- If the file mentioned in the user request does not exist, your plan should include creating it with the appropriate content.
- Your response MUST contain exactly one tool call (write_file) as the final step. No text messages after that.
"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"USER REQUEST: {task_prompt}"}
    ]

    iterations = 0
    try:
        logger.info(f"[Planner] Starting for task {task_id}, prompt: {task_prompt[:100]}...")
        logger.debug(f"[Planner] Workspace: /workspace (inside sandbox)")

        # Log initial file tree to verify sandbox is accessible
        try:
            initial_tree = tools.get_file_tree(".", depth=1)
            logger.debug(f"[Planner] Initial file tree:\n{initial_tree[:500]}")
        except Exception as e:
            logger.warning(f"[Planner] Could not get initial file tree: {e}")

        while iterations < max_iterations:
            iterations += 1
            logger.info(f"[Planner] Iteration {iterations}/{max_iterations}")
            # 🌟 使用重试装饰器自动处理网络错误
            response = _call_openai_with_retry(
                model=MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
                max_tokens=MAX_TOKENS_PER_RESPONSE,
                temperature=0
            )

            assistant_msg = response.choices[0].message
            safe_msg = {"role": "assistant", "content": assistant_msg.content or ""}

            if assistant_msg.tool_calls:
                safe_msg["tool_calls"] = [{"id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}} for t in assistant_msg.tool_calls]
            messages.append(safe_msg)

            num_calls = len(assistant_msg.tool_calls or [])
            logger.info(f"[Planner] Got {num_calls} tool calls")
            if assistant_msg.content:
                logger.info(f"[Planner] Assistant message (first 200 chars): {assistant_msg.content[:200]}")

            if not assistant_msg.tool_calls:
                logger.info(f"[Planner] No tool calls, finishing. Final message: {assistant_msg.content[:200] if assistant_msg.content else '(empty)'}")
                logger.warning(f"[Planner] Stopped without creating task_plan.md! Assistant finished with: {assistant_msg.content[:500] if assistant_msg.content else '(no content)'}")
                break

            for idx, tool_call in enumerate(assistant_msg.tool_calls, 1):
                tool_name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                except json.JSONDecodeError as e:
                    logger.error(f"[Planner] Failed to parse arguments for tool {tool_name}: {e}")
                    args = {}

                logger.info(f"[Planner] Tool {idx}/{num_calls}: {tool_name} with args: {args}")

                handler = TOOL_HANDLERS.get(tool_name)
                if not handler:
                    output = f"Unknown tool: {tool_name}"
                    logger.warning(f"[Planner] {output}")
                else:
                    try:
                        output = str(handler(**args))
                        logger.debug(f"[Planner] Tool {tool_name} returned (first 500 chars): {output[:500]}")
                    except Exception as e:
                        logger.error(f"[Planner] Tool {tool_name} raised exception: {e}", exc_info=True)
                        output = f"Error: {e}"

                messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": tool_name, "content": output[:5000]})

                # 🎯 If planner writes task_plan.md successfully, stop immediately
                if tool_name == "write_file" and args.get("path") == "task_plan.md" and not output.startswith("Error:"):
                    logger.info("[Planner] task_plan.md written, stopping planner")
                    return {"success": True}

            # Increase delay to respect rate limits (10 RPM = min 6s between calls)
            time.sleep(7)

        logger.info(f"[Planner] Completed after {iterations} iterations total")

        # Final check: did we create task_plan.md?
        try:
            check_result = tools.read_file("task_plan.md")
            if check_result and not check_result.startswith("Error:"):
                logger.info(f"[Planner] task_plan.md exists and has {len(check_result)} characters")
            else:
                logger.warning(f"[Planner] task_plan.md not found or error: {check_result}")
        except Exception as e:
            logger.warning(f"[Planner] Could not check for task_plan.md: {e}")

    except Exception as e:
        logger.error(f"Planner API error: {e}", exc_info=True)
        return {"success": False}

    # Final check: did we create task_plan.md?
    try:
        check_result = tools.read_file("task_plan.md")
        if check_result and not check_result.startswith("Error:"):
            logger.info(f"[Planner] task_plan.md exists and has {len(check_result)} characters")
            return {"success": True}
        else:
            logger.warning(f"[Planner] task_plan.md not found or error: {check_result}")
            return {"success": False}
    except Exception as e:
        logger.warning(f"[Planner] Could not check for task_plan.md: {e}")
        return {"success": False}