"""
Feishu Bot - ChatOps Interface for Auto-SWE-Deer

Uses lark-oapi WebSocket client for local development without public IP.
Integrates with the native SuperAgent core for sandboxed SWE task execution.
"""

import asyncio
import logging
import os
import sys
import json
import threading
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from enum import Enum

import lark_oapi as lark
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SWETask:
    task_id: int
    user_id: str
    chat_id: str
    message_id: str
    description: str
    repo_path: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    git_diff: Optional[str] = None


class SWETaskManager:
    def __init__(self, workdir: Path, worktrees_base: Path):
        self.workdir = workdir
        self.worktrees_base = worktrees_base
        self.worktrees_base.mkdir(exist_ok=True)
        self.tasks: dict[int, SWETask] = {}
        self._next_task_id = 1

    def create_task(self, user_id: str, chat_id: str, message_id: str, description: str) -> SWETask:
        task = SWETask(
            task_id=self._next_task_id,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            description=description
        )
        self.tasks[self._next_task_id] = task
        self._next_task_id += 1
        return task

    def get_task(self, task_id: int) -> Optional[SWETask]:
        return self.tasks.get(task_id)

    def update_task(self, task_id: int, **kwargs) -> None:
        if task_id in self.tasks:
            for key, value in kwargs.items():
                if hasattr(self.tasks[task_id], key):
                    setattr(self.tasks[task_id], key, value)


WORKDIR = Path(__file__).parent.parent
WORKTREES_BASE = WORKDIR / ".feishu_worktrees"
WORKTREES_BASE.mkdir(exist_ok=True)

task_manager = SWETaskManager(WORKDIR, WORKTREES_BASE)

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
    logger.warning("FEISHU_APP_ID or FEISHU_APP_SECRET not set in .env")


def send_text_message(chat_id: str, text: str):
    """Send a text message to a chat."""
    try:
        client = lark.Client.builder() \
            .app_id(FEISHU_APP_ID) \
            .app_secret(FEISHU_APP_SECRET) \
            .build()

        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )

        response = client.im.v1.message.create(request)

        if not response.success():
            logger.error(f"Failed to send message: {response.msg}")
        else:
            logger.info("Message sent successfully")

    except Exception as e:
        logger.error(f"Error sending message: {e}")


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    """Callback for im.message.receive_v1 event."""
    try:
        sender = data.event.sender
        message = data.event.message

        user_id = sender.sender_id.open_id
        chat_id = message.chat_id
        message_id = message.message_id

        content_obj = json.loads(message.content)
        text = content_obj.get("text", "").strip() if isinstance(content_obj, dict) else ""

        if not text:
            return

        logger.info(f"Received message from {user_id}: {text[:100]}")

        task = task_manager.create_task(user_id, chat_id, message_id, text)

        threading.Thread(target=process_swe_task, args=(task,), daemon=True).start()

    except Exception as e:
        logger.error(f"Error handling message: {e}")


def process_swe_task(task: SWETask):
    """Process SWE task in a separate thread using native SuperAgent."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(execute_swe_task(task))


async def execute_swe_task(task: SWETask):
    """Execute SWE task using the native SuperAgent with sandbox."""
    import uuid

    from sandbox import SandboxController
    from sandbox.worktree import WorktreeManager
    from agent.super_agent import run_agent_task, extract_git_diff

    task_manager.update_task(task.task_id, status=TaskStatus.RUNNING)

    send_text_message(task.chat_id, "🚀 收到任务！已唤醒原生 Auto-SWE 基座进入睡眠研发模式，正在接管沙盒...")

    sandbox = None
    wt_name = None
    wtm = None

    try:
        wtm = WorktreeManager(WORKDIR / "dummy_repo", WORKTREES_BASE)

        task_manager.update_task(task.task_id, repo_path=str(WORKDIR / "dummy_repo"))

        unique_id = uuid.uuid4().hex[:8]
        wt_name = f"feishu_{unique_id}"

        try:
            existing_wt = wtm.get(wt_name)
            if existing_wt:
                try:
                    wtm.remove(wt_name, force=True)
                except:
                    pass

            wt_info = wtm.create(wt_name, task.task_id, "HEAD")
        except Exception as e:
            logger.error(f"Failed to create worktree: {e}")
            task_manager.update_task(task.task_id, status=TaskStatus.FAILED, result=str(e))
            send_text_message(task.chat_id, f"❌ Worktree创建失败: {str(e)}")
            return

        wt_path = Path(wt_info["path"])

        sandbox = SandboxController()
        result = sandbox.create_container(task.task_id, wt_path)
        logger.info(f"Container created: {result}")

        if not sandbox.is_running():
            logger.error("Container not running")
            task_manager.update_task(task.task_id, status=TaskStatus.FAILED, result="Container failed")
            send_text_message(task.chat_id, "❌ 容器启动失败")
            return

        send_text_message(task.chat_id, "🔍 正在分析代码库并执行SuperAgent研发流程...")

        agent_result = run_agent_task(
            task_prompt=task.description,
            sandbox_controller=sandbox,
            workspace_path=wt_path,
            max_iterations=20
        )

        logger.info(f"Agent completed: iterations={agent_result['iterations']}, success={agent_result['success']}")

        git_diff = extract_git_diff(sandbox)

        task_manager.update_task(
            task.task_id,
            status=TaskStatus.COMPLETED,
            git_diff=git_diff,
            result=agent_result.get("final_message", "")
        )

        send_final_report(task, git_diff)

    except Exception as e:
        logger.error(f"Task #{task.task_id} failed: {e}")
        task_manager.update_task(task.task_id, status=TaskStatus.FAILED, result=str(e))
        send_text_message(task.chat_id, f"❌ 执行失败: {str(e)}")

    finally:
        if sandbox:
            try:
                sandbox.destroy_container()
                logger.info(f"Container destroyed for task #{task.task_id}")
            except Exception as e:
                logger.error(f"Failed to destroy container: {e}")

        if wtm and wt_name:
            try:
                wtm.remove(wt_name, force=True)
                logger.info(f"Worktree '{wt_name}' removed via git")
            except Exception as e:
                logger.warning(f"git remove failed, trying robust_rmtree: {e}")
                try:
                    wt_path = Path(WORKTREES_BASE / wt_name)
                    if wt_path.exists():
                        from sandbox.worktree import robust_rmtree
                        robust_rmtree(str(wt_path), max_retries=3, logger=logger)
                except Exception as rm_err:
                    logger.error(f"Failed to remove worktree '{wt_name}': {rm_err}")


def send_final_report(task: SWETask, git_diff: str):
    """Send the final report with git diff."""
    if len(git_diff) > 4000:
        message = f"✅ SuperAgent研发完成！沙盒已安全销毁。\n\n📊 补丁摘要:\n{git_diff[:4000]}...\n\n(补丁过长，已截断)"
    elif git_diff and git_diff != "(no changes detected)":
        message = f"✅ SuperAgent研发完成！沙盒已安全销毁。\n\n这是本次生成的代码补丁：\n```diff\n{git_diff}\n```"
    else:
        message = "✅ SuperAgent研发完成！沙盒已安全销毁。\n\n(未检测到代码变更)"

    send_text_message(task.chat_id, message)


def main():
    """Main entry point."""
    print("=" * 60)
    print("  Auto-SWE-Deer Feishu Bot (Native SuperAgent)")
    print("=" * 60)

    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        print("\nWARNING: FEISHU_APP_ID or FEISHU_APP_SECRET not set in .env")
        print("Please check your .env file")
        print()

    print("Configuration:")
    print(f"  Model: {os.getenv('OPENAI_MODEL_NAME', 'step-3.5-flash')}")
    print(f"  API Base: {os.getenv('OPENAI_BASE_URL', 'https://api.stepfun.com/v1')}")
    print()

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .build()
    )

    cli = WsClient(
        app_id=FEISHU_APP_ID,
        app_secret=FEISHU_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO
    )

    print("Starting Feishu WebSocket bot...")
    print("Press Ctrl+C to stop")
    print()

    cli.start()


if __name__ == "__main__":
    main()
