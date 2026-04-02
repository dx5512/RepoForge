"""
Task Card Manager for Feishu Integration

Provides a high-level interface for managing task status cards in Feishu.
Reduces boilerplate by encapsulating card state and providing fluent API.
"""

import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

from .feishu_cards import FeishuCardBuilder

logger = logging.getLogger(__name__)


@dataclass
class TaskCardSession:
    """
    Manages the lifecycle of a task's status card in Feishu.

    This class tracks:
    - Task metadata (id, repo, description)
    - Feishu message_id for updates
    - Current card state (status, details, pr_url)

    Provides methods to update status without repeating common parameters.
    """

    task_id: int
    chat_id: str
    message_id: Optional[str] = None
    target_repo: Optional[str] = None
    description: str = ""
    current_status: str = "pending"
    pr_url: Optional[str] = None

    # Status color mapping (same as FeishuCardBuilder.COLORS)
    STATUS_COLORS = {
        "pending": "blue",
        "planning": "purple",
        "running": "wathet",
        "reviewing": "orange",
        "success": "green",
        "failed": "red",
        "no_changes": "gray"  # 新增：无代码变更
    }

    # Status text mapping
    STATUS_TEXTS = {
        "pending": "⏳ 任务排队中...",
        "planning": "🧠 架构师正在分析代码库并制定实施方案...",
        "running": "🏃‍♂️ Agent 正在编写代码...",
        "reviewing": "👀 Reviewer 正在审查代码...",
        "success": "✅ 研发完成，PR 已就绪！",
        "failed": "❌ 任务失败或被终止",
        "no_changes": "⚠️ 未检测到代码变更"  # 新增
    }

    def __post_init__(self):
        """Initialize after dataclass creation."""
        self._feishu_client = None

    def set_message_id(self, message_id: str) -> "TaskCardSession":
        """Set the Feishu message ID after sending initial card."""
        self.message_id = message_id
        return self

    def build_card(self, status: str, details: str, pr_url: Optional[str] = None) -> Dict[str, Any]:
        """
        Build a Feishu card dictionary.

        Args:
            status: Task status (pending, running, success, failed, etc.)
            details: Detailed log/message for this state
            pr_url: Optional PR URL to add a button

        Returns:
            Card dictionary ready for Feishu API
        """
        repo_display = self.target_repo if self.target_repo else "本地沙盒项目"

        status_text = self.STATUS_TEXTS.get(status, "未知状态")
        color = self.STATUS_COLORS.get(status, "blue")

        elements = [
            {
                "tag": "markdown",
                "content": f"**🎯 目标仓库:** `{repo_display}`\n**📝 需求描述:**\n{self.description}"
            },
            {
                "tag": "hr"
            },
            {
                "tag": "markdown",
                "content": f"**📊 当前状态:** <font color='{color}'>{status_text}</font>\n\n**💡 详细日志:**\n{details}"
            }
        ]

        # Add PR button if URL provided
        if pr_url:
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "🔗 点击查看 Pull Request"
                        },
                        "type": "primary",
                        "multi_url": {
                            "url": pr_url,
                            "pc_url": pr_url,
                            "android_url": pr_url,
                            "ios_url": pr_url
                        }
                    }
                ]
            })

        card = {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "template": color,
                "title": {
                    "content": f"🤖 RepoForge 任务 #{self.task_id}",
                    "tag": "plain_text"
                }
            },
            "elements": elements
        }

        return card

    def send_new(self, send_func, status: str, details: str, pr_url: Optional[str] = None) -> Optional[str]:
        """
        Send a new card to the chat.

        Args:
            send_func: Function to send card (e.g., send_interactive_card)
            status: Initial status
            details: Initial details
            pr_url: Optional PR URL

        Returns:
            message_id if successful, None otherwise
        """
        card = self.build_card(status, details, pr_url)
        message_id = send_func(self.chat_id, card)
        if message_id:
            self.message_id = message_id
            self.current_status = status
            self.pr_url = pr_url
            logger.info(f"Task #{self.task_id}: Card sent with message_id={message_id}")
        return message_id

    def update(self, update_func, status: str, details: str, pr_url: Optional[str] = None) -> bool:
        """
        Update an existing card.

        Args:
            update_func: Function to update card (e.g., update_interactive_card)
            status: New status
            details: New details
            pr_url: Optional PR URL (can update existing PR)

        Returns:
            True if update successful, False otherwise
        """
        if not self.message_id:
            logger.warning(f"Task #{self.task_id}: Cannot update card - no message_id")
            return False

        card = self.build_card(status, details, pr_url)
        try:
            update_func(self.message_id, card)
            self.current_status = status
            if pr_url:
                self.pr_url = pr_url
            logger.debug(f"Task #{self.task_id}: Card updated to status={status}")
            return True
        except Exception as e:
            logger.error(f"Task #{self.task_id}: Failed to update card: {e}")
            return False

    def update_status(self, send_func, update_func, status: str, details: str, pr_url: Optional[str] = None) -> bool:
        """
        Convenience method: send new if no message_id, otherwise update.

        Args:
            send_func: Function to send new card
            update_func: Function to update existing card
            status: Status to set
            details: Details to display
            pr_url: Optional PR URL

        Returns:
            True if operation successful
        """
        if self.message_id:
            return self.update(update_func, status, details, pr_url)
        else:
            message_id = self.send_new(send_func, status, details, pr_url)
            return message_id is not None

    def mark_success(self, details: str, pr_url: Optional[str] = None) -> bool:
        """
        Mark task as successful.

        Convenience method that uses the global send/update functions.
        Requires setting session's _send_func and _update_func callbacks.
        """
        if hasattr(self, '_send_func') and hasattr(self, '_update_func'):
            return self.update_status(self._send_func, self._update_func, "success", details, pr_url)
        logger.warning("TaskCardSession: mark_success called but callbacks not set")
        return False

    def mark_failed(self, details: str) -> bool:
        """Mark task as failed."""
        if hasattr(self, '_send_func') and hasattr(self, '_update_func'):
            return self.update_status(self._send_func, self._update_func, "failed", details)
        logger.warning("TaskCardSession: mark_failed called but callbacks not set")
        return False

    def set_callbacks(self, send_func, update_func):
        """Set the send/update functions for convenience methods."""
        self._send_func = send_func
        self._update_func = update_func
        return self


class TaskCardManager:
    """
    Manages multiple task card sessions.

    Provides factory methods to create sessions and central coordination.
    """

    def __init__(self):
        self._sessions: Dict[int, TaskCardSession] = {}

    def create_session(
        self,
        task_id: int,
        chat_id: str,
        target_repo: Optional[str] = None,
        description: str = ""
    ) -> TaskCardSession:
        """Create a new task card session."""
        session = TaskCardSession(
            task_id=task_id,
            chat_id=chat_id,
            target_repo=target_repo,
            description=description
        )
        self._sessions[task_id] = session
        logger.debug(f"TaskCardManager: Created session for task {task_id}")
        return session

    def get_session(self, task_id: int) -> Optional[TaskCardSession]:
        """Get existing session by task_id."""
        return self._sessions.get(task_id)

    def remove_session(self, task_id: int):
        """Remove session after task completion."""
        if task_id in self._sessions:
            del self._sessions[task_id]
            logger.debug(f"TaskCardManager: Removed session for task {task_id}")

    def update_card(
        self,
        task_id: int,
        status: str,
        details: str,
        pr_url: Optional[str] = None
    ) -> bool:
        """Update card for existing task."""
        session = self.get_session(task_id)
        if not session:
            logger.warning(f"TaskCardManager: No session for task {task_id}")
            return False

        # Use global functions if callbacks not set
        from chat_ops.feishu_bot import send_interactive_card, update_interactive_card
        return session.update_status(send_interactive_card, update_interactive_card, status, details, pr_url)


# Global manager instance
global_card_manager = TaskCardManager()


def get_card_manager() -> TaskCardManager:
    """Get the global TaskCardManager instance."""
    return global_card_manager
