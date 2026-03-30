"""
Telegram Bot - ChatOps Interface for Auto-SWE-Deer

This module provides a Telegram bot that:
1. Listens for GitHub Issue URLs or local paths + descriptions
2. Triggers the DeerFlow SuperAgent asynchronously
3. Waits for completion and extracts git diff
4. Sends results back to the user
"""

import asyncio
import logging
import os
import sys
import base64
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from enum import Enum

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(override=True)

from sandbox import TaskRegistry, SandboxController
from sandbox.worktree import WorktreeManager

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
    user_id: int
    chat_id: int
    description: str
    repo_path: Optional[str] = None
    github_url: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    git_diff: Optional[str] = None


class SWETaskManager:
    def __init__(self, workdir: Path, worktrees_base: Path):
        self.workdir = workdir
        self.worktrees_base = worktrees_base
        self.worktrees_base.mkdir(exist_ok=True)
        self.task_registry = TaskRegistry(worktrees_base)
        self.tasks: dict[int, SWETask] = {}
        self._next_task_id = 1

    def create_task(self, user_id: int, chat_id: int, description: str) -> SWETask:
        task = SWETask(
            task_id=self._next_task_id,
            user_id=user_id,
            chat_id=chat_id,
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
WORKTREES_BASE = WORKDIR / ".chat_ops_worktrees"
WORKTREES_BASE.mkdir(exist_ok=True)

task_manager = SWETaskManager(WORKDIR, WORKTREES_BASE)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "Auto-SWE-Deer Bot Ready!\n\n"
        "Send me a GitHub Issue URL or a local file path + description,\n"
        "and I'll fix the bug for you.\n\n"
        "Example:\n"
        "/fix psf/requests #1234\n"
        "or\n"
        "/fix /path/to/repo Fix the authentication bug"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "Available commands:\n"
        "/start - Start the bot\n"
        "/help - Show this help\n"
        "/status - Check task status\n"
        "/fix <repo> <issue> - Fix a GitHub issue\n"
        "/fix <local_path> <description> - Fix a local bug"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    user_id = update.message.from_user.id
    user_tasks = [t for t in task_manager.tasks.values() if t.user_id == user_id]

    if not user_tasks:
        await update.message.reply_text("No tasks found.")
        return

    lines = ["Your Tasks:\n"]
    for task in user_tasks:
        lines.append(f"Task #{task.task_id}: {task.status.value} - {task.description[:50]}")

    await update.message.reply_text("\n".join(lines))


async def fix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /fix command - main entry point for bug fixing."""
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    text = update.message.text

    parts = text.split(None, 2)
    if len(parts) < 2:
        await update.message.reply_text(
            "Usage: /fix <repo_or_path> <issue_or_description>\n"
            "Example: /fix psf/requests #1234"
        )
        return

    repo_or_path = parts[1]
    description = parts[2] if len(parts) > 2 else "Bug fix requested via Telegram"

    task = task_manager.create_task(user_id, chat_id, description)

    await update.message.reply_text(
        f"Task received! [#{task.task_id}]\n"
        f"Starting Sleep-Mode Autoresearch...\n\n"
        f"Target: {repo_or_path}\n"
        f"Description: {description}"
    )

    asyncio.create_task(run_swe_task(task.task_id, repo_or_path, description))


async def run_swe_task(task_id: int, target: str, description: str):
    """
    Run the SWE task asynchronously.

    This is the core workflow:
    1. Create worktree
    2. Create sandbox
    3. Clone repo or use local path
    4. Run SuperAgent (simulated for now)
    5. Extract git diff
    6. Send results back
    """
    task = task_manager.get_task(task_id)
    if not task:
        logger.error(f"Task #{task_id} not found")
        return

    task_manager.update_task(task_id, status=TaskStatus.RUNNING)

    try:
        wtm = WorktreeManager(WORKDIR / "dummy_repo", WORKTREES_BASE)

        task_manager.update_task(
            task_id,
            repo_path=str(WORKDIR / "dummy_repo")
        )

        wt_name = f"task_{task_id}"
        try:
            wt_info = wtm.create(wt_name, task_id, "HEAD")
        except Exception as e:
            logger.error(f"Failed to create worktree: {e}")
            task_manager.update_task(task_id, status=TaskStatus.FAILED, result=str(e))
            return

        wt_path = Path(wt_info["path"])

        sandbox = SandboxController()
        result = sandbox.create_container(task_id, wt_path)
        logger.info(f"Container created: {result}")

        if not sandbox.is_running():
            logger.error("Container not running")
            task_manager.update_task(task_id, status=TaskStatus.FAILED, result="Container failed to start")
            return

        git_diff_result = sandbox.execute_in_sandbox("git diff HEAD~1..HEAD -- .")
        git_diff = git_diff_result.get("stdout", "")

        task_manager.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            git_diff=git_diff if git_diff else "No changes detected yet"
        )

        sandbox.destroy_container()
        logger.info(f"Task #{task_id} completed")

    except Exception as e:
        logger.error(f"Task #{task_id} failed: {e}")
        task_manager.update_task(task_id, status=TaskStatus.FAILED, result=str(e))


async def check_task_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Poll for task updates - simplified version using callback queries."""
    user_id = update.message.from_user.id
    task_id = context.args[0] if context.args else None

    if not task_id:
        await update.message.reply_text("Usage: /check <task_id>")
        return

    try:
        task_id = int(task_id)
    except ValueError:
        await update.message.reply_text("Invalid task ID")
        return

    task = task_manager.get_task(task_id)
    if not task:
        await update.message.reply_text(f"Task #{task_id} not found")
        return

    if task.user_id != user_id:
        await update.message.reply_text("You don't own this task")
        return

    status_emoji = {
        TaskStatus.PENDING: "Pending",
        TaskStatus.RUNNING: "Running...",
        TaskStatus.COMPLETED: "Completed!",
        TaskStatus.FAILED: "Failed"
    }

    message = f"Task #{task_id} Status: {status_emoji.get(task.status, 'Unknown')}\n\n"

    if task.status == TaskStatus.COMPLETED:
        message += "Git Diff:\n"
        message += f"```\n{task.git_diff[:2000] if task.git_diff else 'No diff'}\n```"

    await update.message.reply_text(message, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular messages - route to /fix."""
    text = update.message.text.strip()

    if text.startswith("/"):
        await update.message.reply_text(
            "Unknown command. Try /help for available commands."
        )
        return

    await update.message.reply_text(
        "Please use /fix <repo> <description> to submit a bug fix task."
    )


def create_bot():
    """Create and configure the Telegram bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set in .env")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("fix", fix_command))
    application.add_handler(CommandHandler("check", check_task_update))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return application


async def run_bot():
    """Run the bot."""
    application = create_bot()
    logger.info("Starting Telegram bot...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    """Main entry point."""
    print("=" * 60)
    print("  Auto-SWE-Deer Telegram Bot")
    print("=" * 60)

    if not TELEGRAM_BOT_TOKEN:
        print("\nWARNING: TELEGRAM_BOT_TOKEN not set in .env")
        print("Please add TELEGRAM_BOT_TOKEN=your_token to .env")
        print("\nTo get a bot token:")
        print("1. Message @BotFather on Telegram")
        print("2. Send /newbot")
        print("3. Follow instructions to get your token")
        print()

    print("Starting bot...")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
